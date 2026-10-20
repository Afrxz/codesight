"""The agent's run loop: fetch -> recall -> decide -> remember -> report.

For every tracked card:
    1. fetch today's MarketData from the configured source (mock or real)
    2. read its recent history from memory
    3. decide (one LLM call, or rules if no LLM is configured)
    4. append today's snapshot to memory
Then write all decisions to data/decisions.json for the dashboard.

Deliberately a plain loop — no orchestration framework. Each card is handled
independently, and a failure on one card is logged and skipped rather than
aborting the whole run.

Usage:
    python -m src.run                               # use the saved settings (mock by default)
    python -m src.run --source mock                 # LLM if LLM_API_KEY is set, else rules
    python -m src.run --source mock --llm off       # force rule-based decisions
    python -m src.run --source pokemontcg           # free TCGplayer prices for raw cards
    python -m src.run --source tcgapi --limit 3     # tcgapi.net ("real" also works)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .db import init_db
from . import alerts, evaluate, ledger, portfolio
from .eval import trace
from .decide import AgentContext, Decision, LLMFn, decide, make_chat, make_llm
from .manage import list_cards
from .market.base import MarketSource, TransientSourceError
from .market.mock import MockSource
from .market.types import MarketData
from .memory import get_history
from .tools import RunBudget, Toolbox

log = logging.getLogger("tcg.run")


SOURCES = ("mock", "pokemontcg", "justtcg", "tcgapi", "mcp")
# "real" was the plan's name for the tcgapi.net source; keep it working.
SOURCE_ALIASES = {"real": "tcgapi"}


def canonical_source(name: str) -> str:
    name = SOURCE_ALIASES.get(name, name)
    if name not in SOURCES:
        raise ValueError(f"unknown source {name!r} (expected one of {', '.join(SOURCES)})")
    return name


def make_source(name: str) -> MarketSource:
    """The one place a source is chosen. Everything downstream sees MarketSource."""
    name = canonical_source(name)
    if name == "mock":
        return MockSource()
    if name == "pokemontcg":
        from .market.pokemontcg_source import PokemonTcgSource  # free, no key needed
        return PokemonTcgSource()
    if name == "justtcg":
        from .market.justtcg import JustTcgSource  # needs JUSTTCG_API_KEY (free plan)
        return JustTcgSource()
    if name == "mcp":
        from .market.mcp_source import McpSource  # prices through our MCP server (Layer C)
        return McpSource()
    from .market.tcgapi import TcgApiSource  # needs TCGAPI_KEY; import lazily
    return TcgApiSource()


def resolve_llm(mode: str) -> LLMFn | None:
    """'on' requires a key, 'off' never uses one, 'auto' uses one if present."""
    if mode == "off":
        return None
    try:
        return make_llm(config.get_settings())
    except config.ConfigError:
        if mode == "on":
            raise
        log.warning("LLM_API_KEY not set; using rule-based decisions (decided_by='rules').")
        return None


def resolve_agent(llm_mode: str, source: MarketSource, source_name: str) -> AgentContext | None:
    """The tool-using agent loop, when an LLM is available and AGENT_TOOLS is on."""
    budget = int(config.get("AGENT_TOOL_BUDGET"))
    if llm_mode == "off" or config.get("AGENT_TOOLS") != "on" or budget <= 0:
        return None
    try:
        chat = make_chat(config.get_settings())
    except config.ConfigError:
        return None
    run_budget = RunBudget()   # network look-ups shared by every card in this run
    return AgentContext(
        chat=chat,
        toolbox=lambda card, market: Toolbox(card=card, market=market, source_name=source_name,
                                             source=source, budget=run_budget),
        budget=budget,
    )


def _previous_run(path: Path, source_name: str) -> dict[int, dict]:
    """Last run's decisions by card id (same source only), for change detection."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("source") != source_name:
        return {}
    return {d["card_id"]: d for d in payload.get("decisions", [])}


def run(
    source_name: str | None = None,
    llm_mode: str | None = None,
    limit: int | None = None,
    out_path: Path | None = None,
    use_agent: bool = True,
    started_by: str = "cli",
) -> list[Decision]:
    """One full agent cycle over all tracked cards, checkpointed card by card.

        observe   fetch today's prices (batched where the source allows)
        reason    decide each card: tool-using LLM -> single LLM call -> rules
        remember  price snapshot + the recommendation itself (for self-grading),
                  written with the card's checkpoint in one transaction
        review    collection-level briefing and budget plan
        alert     detect meaningful changes, store them, send to the webhook

    Durable (src/ledger.py): if a previous run on the same source was
    interrupted, this resumes it (only its unfinished cards, with its original
    settings) instead of starting over. A card that keeps failing is marked
    failed and the run carries on.

    `source_name` / `llm_mode` default to the saved settings (RUN_SOURCE,
    RUN_LLM), so the binder's Re-run button, the daily schedule and the CLI
    all behave the same.
    """
    source_name = canonical_source(source_name or config.get("RUN_SOURCE"))
    init_db()

    # Resume an interrupted run, or start a new one.
    interrupted = ledger.find_interrupted()
    if interrupted and interrupted["source"] != source_name:
        # A different source is a different price series; don't mix them.
        ledger.finish(interrupted["run_id"], ledger.FAILED,
                      error=f"Interrupted, then superseded by a run on {source_name}.")
        interrupted = None
    if interrupted:
        run_id, llm_mode = interrupted["run_id"], interrupted["llm_mode"]
        by_id = {c["id"]: c for c in list_cards()}
        cards = [by_id[i] for i in ledger.card_ids(run_id) if i in by_id]
        todo = [by_id[i] for i in ledger.card_ids(run_id, "pending") if i in by_id]
        log.info("Resuming interrupted run %s: %d of %d cards left", run_id, len(todo), len(cards))
    else:
        llm_mode = llm_mode or config.get("RUN_LLM")
        cards = list_cards()[:limit] if limit else list_cards()
        run_id = ledger.start_run(cards, source_name, llm_mode, started_by)
        todo = cards

    try:
        decisions = _process(run_id, cards, todo, source_name, llm_mode, use_agent,
                             out_path or config.DATA_DIR / "decisions.json")
    except Exception as exc:
        # Something outside the per-card steps broke: the run can't complete.
        # (A kill or Ctrl+C never reaches here, so that run stays resumable.)
        ledger.finish(run_id, ledger.FAILED, error=f"{type(exc).__name__}: {exc}")
        raise
    row = ledger.get_run(run_id)
    all_failed = row["cards_total"] > 0 and row["cards_done"] == 0
    ledger.finish(run_id, ledger.FAILED if all_failed else ledger.COMPLETED,
                  error="Every card failed; see the run's progress for each error." if all_failed else None)
    return decisions


def _process(run_id: str, cards: list, todo: list, source_name: str, llm_mode: str,
             use_agent: bool, out_path: Path) -> list[Decision]:
    """Build the run's source, do the work, and always release the source (e.g. the
    MCP server process behind McpSource)."""
    source = make_source(source_name)
    try:
        return _work(run_id, cards, todo, source, price_series(source_name, source),
                     llm_mode, use_agent, out_path)
    finally:
        close = getattr(source, "close", None)
        if close:
            close()


def price_series(source_name: str, source: MarketSource) -> str:
    """Which price series a run belongs to, for history, recommendations and the binder.

    Normally the source's own name. Through the MCP server it's the source the
    server wraps: `--source mcp` changes how prices travel, not what they are,
    so its snapshots continue the same trend (e.g. "justtcg").
    """
    return getattr(source, "upstream", None) or source_name


def _work(run_id: str, cards: list, todo: list, source: MarketSource, series: str, llm_mode: str,
          use_agent: bool, out_path: Path) -> list[Decision]:
    """The work of one run: every card in `todo`, then the collection-level steps."""
    llm = resolve_llm(llm_mode)
    agent = resolve_agent(llm_mode, source, series) if (use_agent and llm) else None
    previous = _previous_run(out_path, series)
    log.info("Run %s: %d of %d cards to do, source=%s, llm=%s, tools=%s",
             run_id, len(todo), len(cards), series, "on" if llm else "off",
             f"up to {agent.budget}/card" if agent else "off")

    # observe
    try:
        source.prefetch(todo)    # batch-capable sources load everything in a few requests
    except Exception:
        log.exception("Prefetch failed; falling back to per-card lookups")

    # The agent's own 30-day track record, once there's enough of it.
    track = evaluate.prompt_summary()
    extra_facts = {"agent_track_record_30d": track} if track else None

    for card in todo:
        try:
            market, attempts = fetch_with_retry(source, card)
            # Read BEFORE writing today's row, and only this series' history:
            # mock and real prices are different series.
            history = get_history(card["id"], source=series)
            effective = with_manual_price(card, market)
            decision = decide(card, effective, history, llm, agent=agent, extra_facts=extra_facts)
        except Exception as exc:
            # One bad card (API outage, unexpected data) shouldn't sink the run:
            # record why, and carry on with the next one.
            log.exception("Card %s (%s) failed", card["id"], card["name"])
            ledger.checkpoint_failed(run_id, card["id"], f"{type(exc).__name__}: {exc}",
                                     attempts=getattr(exc, "fetch_attempts", 1))
            continue
        # remember: snapshot (market data only, never your own estimate) +
        # recommendation + checkpoint, atomically.
        ledger.checkpoint_done(run_id, card["id"], market, effective, decision, series, attempts)

    # Everything done in this run, including cards finished before a resume.
    decisions, markets = ledger.results(run_id)

    # review: the whole collection
    review = portfolio.review(decisions, cards, llm)
    write_decisions(decisions, series, out_path, markets, review)

    # alert: only what changed and matters
    try:
        found = alerts.detect(cards, decisions, previous, series) + alerts.detect_dips(cards, markets)
        saved = alerts.store(found)
        sent = alerts.send(saved)
        if saved:
            log.info("%d new alert(s)%s", len(saved), f", {sent} sent to webhook" if sent else "")
    except Exception:
        log.exception("Alert step failed")

    log.info("Run %s finished: %d/%d decisions written to %s", run_id, len(decisions), len(cards), out_path)
    return decisions


# Run-level retry for a card's fetch (Phase 8.3). The live sources already
# retry HTTP 429/5xx inside each request; this catches what still escapes a
# source (a dropped connection, a timeout, an outage) and tries the whole
# fetch again with exponential backoff before giving up on the card.
FETCH_ATTEMPTS = 3            # 1 try + 2 retries
BACKOFF_BASE_S = 1.0          # waits 1s, then 2s
_backoff_sleep = time.sleep   # replaced in tests so they don't wait


def is_transient(exc: Exception) -> bool:
    """Worth retrying? Outages and timeouts yes; a bad card or a bug no."""
    import requests

    if isinstance(exc, (TransientSourceError, requests.ConnectionError, requests.Timeout,
                        ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def fetch_with_retry(source: MarketSource, card) -> tuple[MarketData, int]:
    """fetch() with retry + exponential backoff on transient errors. Returns (market, attempts).

    A non-transient error is raised straight away (retrying a bug only wastes
    API credits); a transient one after the last attempt. Each attempt is its
    own fetch step in an eval trace, error included.
    """
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return fetch(source, card), attempt
        except Exception as exc:
            if not is_transient(exc) or attempt == FETCH_ATTEMPTS:
                exc.fetch_attempts = attempt     # for the ledger: how many tries were made
                raise
            delay = BACKOFF_BASE_S * 2 ** (attempt - 1)
            log.warning("Fetch for %s failed (%s: %s); retry %d of %d in %.0fs",
                        card["name"], type(exc).__name__, exc, attempt, FETCH_ATTEMPTS - 1, delay)
            _backoff_sleep(delay)
    raise AssertionError("unreachable")


def fetch(source: MarketSource, card) -> MarketData:
    """Fetch one card's market data (traced as a tool step when the eval harness is watching)."""
    args = (card["name"], card["set_code"], card["grade"], card["variant"], card["catalog_id"],
            card["condition"])
    tracer = trace.current()
    if tracer is None:
        return source.fetch_market_data(*args)
    names = ("name", "set_code", "grade", "variant", "catalog_id", "condition")
    with tracer.tool_step("fetch_market_data", dict(zip(names, args))) as slot:
        market = source.fetch_market_data(*args)
        slot["result"] = market.model_dump(mode="json")
    return market


def with_manual_price(card, market: MarketData) -> MarketData:
    """Fallback: when the source has no price, use the owner's own price, if set.

    Market data always wins when it exists; your price only fills the gap
    (e.g. a set too new for free sources). It's flagged `manual`, so the
    decision layer keeps confidence low and says where the number came from.
    """
    if market.sold_avg is not None or card["manual_price"] is None:
        return market
    why = f" {market.source_note}" if market.source_note else ""
    return market.model_copy(update={
        "sold_avg": card["manual_price"],
        "sold_count": None,
        "manual": True,
        "source_note": f"Using your own price, set on {card['manual_price_date']}, "
                       f"because the market source has none.{why}",
    })


def refresh_card(card_id: int) -> Decision | None:
    """Re-decide one card right away (after it's added or its price changes).

    A full run would re-fetch every card and add a history snapshot for each;
    this updates just one card's entry in decisions.json, using the latest
    run's source, and writes no history. Returns None if it couldn't (the card
    still exists, it just keeps its old recommendation until the next run).
    """
    path = config.DATA_DIR / "decisions.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        source_name = canonical_source(payload.get("source") or config.get("RUN_SOURCE"))
        card = next((c for c in list_cards() if c["id"] == card_id), None)
        if card is None:
            return None
        market = with_manual_price(card, fetch(make_source(source_name), card))
        llm = resolve_llm(config.get("RUN_LLM"))
        decision = decide(card, market, get_history(card_id, source=source_name), llm)
    except Exception:
        log.exception("Couldn't refresh card %s", card_id)
        return None

    payload.setdefault("generated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    payload["source"] = source_name
    payload["decisions"] = [d for d in payload.get("decisions", []) if d["card_id"] != card_id]
    payload["decisions"].append(decision.model_dump())
    payload.setdefault("markets", {})[str(card_id)] = market.model_dump(mode="json")
    _write_json(path, payload)
    return decision


def write_decisions(
    decisions: list[Decision], source_name: str, path: Path,
    markets: dict[int, MarketData] | None = None, review: dict | None = None,
) -> None:
    _write_json(path, {
        "portfolio": review,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source_name,
        "decisions": [d.model_dump() for d in decisions],
        # The evidence behind each decision (sample listings + sold comps),
        # keyed by card id, for the binder's detail view. JSON keys are strings.
        "markets": {str(k): m.model_dump(mode="json") for k, m in (markets or {}).items()},
    })


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then rename, so the binder never reads a half-written file.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def print_table(decisions: list[Decision]) -> None:
    def num(v, fmt):
        return format(v, fmt) if v is not None else "-"

    header = (f"{'id':>3}  {'name':<24} {'status':<8} {'verdict':<7} {'conf':<6} "
              f"{'best':>8} {'sold_avg':>8} {'vs_mkt':>7} {'trend':<7} {'pnl%':>7}  by")
    print(header)
    print("-" * len(header))
    for d in decisions:
        print(f"{d.card_id:>3}  {d.name:<24} {d.status:<8} {d.verdict.upper():<7} "
              f"{d.confidence:<6} {num(d.best_listing, '8.2f'):>8} {num(d.sold_avg, '8.2f'):>8} "
              f"{num(d.vs_market_pct, '+.1f'):>7} {d.trend_30d:<7} "
              f"{num(d.unrealized_pnl_pct, '+.1f'):>7}  {d.decided_by}")
    print()
    for d in decisions:
        print(f"[{d.verdict.upper()}] {d.name}: {d.reason}")


def setup_logging() -> None:
    """Log to the console and to data/run.log (so scheduled runs leave a trail)."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(config.DATA_DIR / "run.log", encoding="utf-8")],
    )
    # The HTTP client libraries are chatty at INFO.
    for noisy in ("httpx", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the TCG market agent once.")
    parser.add_argument("--source", choices=(*SOURCES, *SOURCE_ALIASES),
                        help="default: the saved RUN_SOURCE setting (mock unless changed)")
    parser.add_argument("--llm", choices=("auto", "on", "off"),
                        help="auto: use the LLM if LLM_API_KEY is set. "
                             "Default: the saved RUN_LLM setting (auto unless changed)")
    parser.add_argument("--limit", type=int, help="only process the first N cards")
    args = parser.parse_args(argv)

    setup_logging()
    decisions = run(args.source, args.llm, args.limit)
    print()
    print_table(decisions)


if __name__ == "__main__":
    main()
