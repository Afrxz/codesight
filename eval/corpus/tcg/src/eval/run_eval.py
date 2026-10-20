"""Phase 7.5 — the eval runner (pass@k).

    python -m src.eval.run_eval --k 3                    # rules engine: deterministic, free
    python -m src.eval.run_eval --k 3 --llm on           # the real LLM agent with tools
    python -m src.eval.run_eval --k 3 --llm on --judge   # + LLM-as-judge on the reasons
    python -m src.eval.run_eval --variant prompt-v2 ...  # label the run for regression diffs

For every case, k attempts each run the *production* path (traced_decision:
fetch -> your-price fallback -> decide, including the tool loop and every
guardrail) on the case's frozen data, then apply the graders and detectors.

Raw results go to data/eval/<run_id>/:
    meta.json       what was run (variant, k, model, pinned settings, prompt hash)
    results.jsonl   one line per case per attempt: grades, detections, judge, full trace
    summary.json    the metrics (written by report.py at the end)

Reproducibility:
- Decision settings (thresholds, tool budget) are pinned to their defaults for
  the run, so your own Settings can't silently change eval results.
- The data is frozen per case; with --llm off the whole run is deterministic.
- With --llm on, attempts differ (that's what pass@k measures); the report is a
  pure function of results.jsonl, so it can always be recomputed exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..decide import AgentContext, SYSTEM_PROMPT_TEMPLATE, make_chat, make_llm
from ..tools import RunBudget
from .cases import EvalCase, FixtureSource, FixtureToolbox, load_suite
from .detectors import detect
from .graders import grade, judge
from .trace import traced_decision

log = logging.getLogger("tcg.eval")

# Settings that change decisions; pinned to defaults so runs are comparable.
PINNED = ("BUY_EDGE_PCT", "STRONG_EDGE_PCT", "BIG_GAIN_PCT", "TREND_THRESHOLD_PCT",
          "AGENT_TOOLS", "AGENT_TOOL_BUDGET")


def eval_dir() -> Path:
    return config.DATA_DIR / "eval"


@contextmanager
def pinned_settings():
    """Use the default decision settings for the duration of the run."""
    saved = {k: os.environ.get(k) for k in PINNED}
    try:
        for key in PINNED:
            os.environ.pop(key, None)            # unset -> config.get returns the default
        yield {key: config.get(key) for key in PINNED}
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def prompt_fingerprint() -> str:
    """Short hash of the decision prompt, so runs record which prompt they used."""
    return hashlib.sha256(SYSTEM_PROMPT_TEMPLATE.encode()).hexdigest()[:12]


def run_attempt(case: EvalCase, attempt: int, card_id: int, llm, chat, use_judge: bool) -> dict:
    """One attempt at one case -> a JSON-ready result row."""
    card = case.card_row(card_id)
    agent = None
    if chat is not None and config.get("AGENT_TOOLS") == "on" and int(config.get("AGENT_TOOL_BUDGET")) > 0:
        agent = AgentContext(
            chat=chat,
            toolbox=lambda c, m: FixtureToolbox.for_case(case, c, m, RunBudget()),
            budget=int(config.get("AGENT_TOOL_BUDGET")))
    decision, trace = traced_decision(case.task_id, card, FixtureSource(case), case.history_rows(),
                                      llm, agent=agent)
    grades = grade(case, trace.final_decision)
    if use_judge:
        grades.judge = judge(case, trace.final_decision, llm)
    return {
        "task_id": case.task_id, "category": case.category, "attempt": attempt,
        "passed": grades.passed,
        "grades": [r.model_dump() for r in grades.results],
        "judge": grades.judge,
        "detections": [d.model_dump() for d in detect(trace)],
        # With an LLM configured, a "rules" decision means the LLM path failed and
        # production's fallback answered: counted separately so it can't hide.
        "llm_fallback": llm is not None and (trace.final_decision or {}).get("decided_by") == "rules"
                        and case.market.sold_avg is not None,
        "trace": trace.model_dump(),
    }


def run_eval(k: int = 3, llm_mode: str = "off", variant: str | None = None, use_judge: bool = False,
             workers: int = 4, cases: list[EvalCase] | None = None) -> Path:
    """Run the suite k times per case and write raw results. Returns the run folder."""
    cases = cases if cases is not None else load_suite()
    llm = chat = None
    model = None
    if llm_mode == "on":
        settings = config.get_settings()           # raises a clear error without an LLM key
        llm, chat, model = make_llm(settings), make_chat(settings), settings.llm_model
    variant = variant or ("llm" if llm else "rules")
    started = datetime.now(timezone.utc)
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{variant}"
    out = eval_dir() / run_id
    out.mkdir(parents=True, exist_ok=True)

    with pinned_settings() as pinned:
        jobs = [(case, attempt, index) for index, case in enumerate(cases, 1) for attempt in range(1, k + 1)]
        log.info("Eval %s: %d cases x k=%d = %d attempts (llm=%s)", run_id, len(cases), k, len(jobs), llm_mode)
        # LLM attempts are I/O-bound, so a few run in parallel; each thread has its
        # own tracer (context variables are per-thread).
        with ThreadPoolExecutor(max_workers=workers if llm else 1) as pool:
            rows = list(pool.map(lambda j: run_attempt(j[0], j[1], j[2], llm, chat, use_judge), jobs))

    rows.sort(key=lambda r: (r["task_id"], r["attempt"]))
    with (out / "results.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    meta = {
        "run_id": run_id, "variant": variant, "k": k, "llm": llm_mode, "model": model,
        "judge": use_judge, "cases": len(cases), "attempts": len(rows),
        "prompt_sha": prompt_fingerprint(), "pinned_settings": pinned,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the eval suite (pass@k).")
    parser.add_argument("--k", type=int, default=3, help="attempts per case (default 3)")
    parser.add_argument("--llm", choices=("off", "on"), default="off",
                        help="off: the rules engine (deterministic); on: the LLM agent with tools")
    parser.add_argument("--variant", help="label for this run (e.g. baseline, prompt-v2)")
    parser.add_argument("--judge", action="store_true", help="also score reasons with the LLM judge")
    parser.add_argument("--workers", type=int, default=4, help="parallel LLM attempts")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("httpx", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    out = run_eval(args.k, args.llm, args.variant, args.judge, args.workers)

    from .report import write_report
    summary = write_report(out)
    print(f"\nRaw results: {out}")
    print(f"Summary:     {out / 'summary.json'}  (success {summary['success_rate']:.1%}, "
          f"pass@{args.k} {summary['pass_at_k']:.1%})")


if __name__ == "__main__":
    main()
