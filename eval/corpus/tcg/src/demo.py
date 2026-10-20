"""Build a demo dataset: seed cards + a month of mock price history + one run.

    python -m src.demo            # 30 days of history
    python -m src.demo --days 14

Real history only accumulates one run per day, so a fresh install would show
flat, single-point charts. This fills in what a month of daily mock runs would
have recorded (MockSource with `as_of` = each past day), then does a normal
run for today. Mock-only by design: it never invents history for real data.

WARNING: this wipes existing cards and history (it reseeds).
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timedelta, timezone

from . import evaluate
from .db import get_connection
from .decide import Decision, decide
from .manage import list_cards, seed
from .market.mock import MockSource
from .memory import append_snapshot
from .run import print_table, run, setup_logging

log = logging.getLogger("tcg.demo")


def build_demo(days: int = 30, llm_mode: str = "off") -> list[Decision]:
    """Reseed, then replay `days` past daily runs on mock data, then run today.

    Each past day gets a price snapshot AND a rule-based recommendation made
    with only the history known on that day, so the agent's track record
    (src/evaluate.py) has a month of calls to grade straight away.
    """
    seed(force=True)
    with get_connection() as conn:   # start the demo with no old alerts (recommendations go with their cards)
        conn.execute("DELETE FROM alerts")
    today = date.today()
    cards = list_cards()
    history: dict[int, list[dict]] = {c["id"]: [] for c in cards}
    # Stop one day short of the 30-day window edge so every row stays visible.
    for days_ago in range(min(days, 29), 0, -1):
        as_of = today - timedelta(days=days_ago)
        source = MockSource(as_of=as_of)
        run_at = datetime.combine(as_of, time(9, 0), tzinfo=timezone.utc).isoformat()
        decisions = []
        for card in cards:
            market = source.fetch_market_data(card["name"], card["set_code"], card["grade"])
            decisions.append(decide(card, market, history[card["id"]]))   # rules, as of that day
            append_snapshot(card["id"], market, run_at=run_at)
            history[card["id"]].append({"run_at": run_at, "sold_avg": market.sold_avg})
        evaluate.record(decisions, "mock", made_at=run_at)
    log.info("Backfilled %d days of mock history and calls for %d cards", min(days, 29), len(cards))
    return run("mock", llm_mode)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Reseed and backfill demo history (mock only).")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--llm", choices=("auto", "on", "off"), default="auto")
    args = parser.parse_args(argv)
    setup_logging()
    print_table(build_demo(args.days, args.llm))


if __name__ == "__main__":
    main()
