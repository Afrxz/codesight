"""Phase 7.6 — metrics + report, computed only from a run's raw results.

    python -m src.eval.report                 # the latest run
    python -m src.eval.report <run_id>        # a specific run

Metrics (all derived from results.jsonl, so they can always be recomputed):

    success_rate            attempts that passed every applicable grader / all attempts
    pass_at_k               cases where at least one of the k attempts passed
    pass_all_k              cases where all k attempts passed (consistency; "pass^k")
    by_category             success rate per category
    failure_modes           share of attempts each detector fired on
    tool_call_failure_rate  tool calls that errored (exceptions, rejected or invalid
                            calls) / all tool calls, incl. the market-data fetch
    llm_fallback_rate       LLM-configured attempts that silently fell back to the
                            rules (would otherwise inflate an "LLM" score)
    guardrail_overrides     share of attempts where each code guardrail changed the
                            verdict, so a pass the guardrail earned isn't credited
                            to the model
    avg_cost_usd / avg_latency_ms / avg_steps / avg_tokens   per attempt
    judge                   mean LLM-as-judge scores, if the run used --judge

Latency is reported but not part of reproducibility (it's wall-clock time).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from .detectors import DETECTOR_NAMES

# Metrics that depend on wall-clock time, excluded when comparing two runs for reproducibility.
TIMING_METRICS = ("avg_latency_ms",)


def load_rows(run_dir: Path) -> list[dict]:
    with (run_dir / "results.jsonl").open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 6) if values else None


def summarize(rows: list[dict], meta: dict | None = None) -> dict:
    """All metrics for one run, from its raw result rows."""
    by_case: dict[str, list[dict]] = defaultdict(list)
    by_category: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_case[r["task_id"]].append(r)
        by_category[r["category"]].append(r["passed"])

    tool_steps = [s for r in rows for s in r["trace"]["steps"] if s["type"] == "tool_call"]
    # The model's own look-ups: not the harness's fetch, and not the final submit_decision.
    model_tool_steps = [s for s in tool_steps if s["tool_name"] not in ("fetch_market_data", "submit_decision")]
    traces = [r["trace"] for r in rows]
    costs = [t["total_cost_usd"] for t in traces]
    grader_failures: dict[str, int] = defaultdict(int)
    for r in rows:
        for g in r["grades"]:
            if g["applicable"] and not g["passed"]:
                grader_failures[g["grader"]] += 1

    guardrails: dict[str, int] = defaultdict(int)
    for t in traces:
        for name in (t.get("final_decision") or {}).get("guardrails", []):   # absent in older runs
            guardrails[name] += 1

    judged = [r["judge"] for r in rows if r.get("judge")]
    n = len(rows) or 1
    return {
        "run_id": (meta or {}).get("run_id"),
        "variant": (meta or {}).get("variant"),
        "k": (meta or {}).get("k"),
        "cases": len(by_case),
        "attempts": len(rows),
        "success_rate": round(sum(r["passed"] for r in rows) / n, 4),
        "pass_at_k": round(sum(any(a["passed"] for a in att) for att in by_case.values()) / (len(by_case) or 1), 4),
        "pass_all_k": round(sum(all(a["passed"] for a in att) for att in by_case.values()) / (len(by_case) or 1), 4),
        "by_category": {cat: round(sum(v) / len(v), 4) for cat, v in sorted(by_category.items())},
        "failed_cases": sorted(t for t, att in by_case.items() if not all(a["passed"] for a in att)),
        "grader_failures": dict(sorted(grader_failures.items())),
        "failure_modes": {name: round(sum(any(d["detector"] == name and d["fired"] for d in r["detections"])
                                          for r in rows) / n, 4) for name in DETECTOR_NAMES},
        "tool_calls": len(tool_steps),
        "tool_call_failure_rate": round(sum(bool(s["error"]) for s in tool_steps) / (len(tool_steps) or 1), 4),
        "model_tool_calls_per_attempt": round(len(model_tool_steps) / n, 3),
        "llm_fallback_rate": round(sum(bool(r.get("llm_fallback")) for r in rows) / n, 4),
        "guardrail_overrides": {name: round(count / n, 4) for name, count in sorted(guardrails.items())},
        "avg_cost_usd": _mean(costs) if all(c is not None for c in costs) else None,
        "avg_latency_ms": _mean(t["wall_ms"] for t in traces),
        "avg_steps": _mean(len(t["steps"]) for t in traces),
        "avg_tokens": _mean(t["total_tokens"] for t in traces),
        "judge": {"cites_numbers": _mean(j["cites_numbers"] for j in judged),
                  "justified": _mean(j["justified"] for j in judged),
                  "judged": len(judged)} if judged else None,
    }


def print_summary(s: dict) -> None:
    pct = lambda v: "n/a" if v is None else f"{v:.1%}"
    num = lambda v, fmt="{:.1f}": "n/a" if v is None else fmt.format(v)
    print(f"\nEval run {s['run_id']}  (variant: {s['variant']}, k={s['k']}, "
          f"{s['cases']} cases, {s['attempts']} attempts)")
    print("-" * 64)
    rows = [
        ("Success rate (per attempt)", pct(s["success_rate"])),
        (f"pass@{s['k']} (any attempt passed)", pct(s["pass_at_k"])),
        (f"pass^{s['k']} (all attempts passed)", pct(s["pass_all_k"])),
        ("Tool-call failure rate", f"{pct(s['tool_call_failure_rate'])}  of {s['tool_calls']} calls"),
        ("LLM fallback to rules", pct(s["llm_fallback_rate"])),
        ("Avg look-ups the model chose / attempt", num(s["model_tool_calls_per_attempt"], "{:.2f}")),
        ("Avg steps / tokens", f"{num(s['avg_steps'])} / {num(s['avg_tokens'], '{:.0f}')}"),
        ("Avg latency", f"{num(s['avg_latency_ms'], '{:.0f}')} ms"),
        ("Avg cost", "not configured" if s["avg_cost_usd"] is None else f"${s['avg_cost_usd']:.5f}"),
    ]
    for label, value in rows:
        print(f"  {label:34} {value}")
    print("  By category:")
    for cat, rate in s["by_category"].items():
        print(f"    {cat:32} {pct(rate)}")
    print("  Failure modes (share of attempts):")
    for name, rate in s["failure_modes"].items():
        print(f"    {name:32} {pct(rate)}")
    if s.get("guardrail_overrides"):
        print("  Guardrail changed the verdict:",
              ", ".join(f"{k} {pct(v)}" for k, v in s["guardrail_overrides"].items()))
    if s["grader_failures"]:
        print("  Grader failures:", ", ".join(f"{k} {v}" for k, v in s["grader_failures"].items()))
    if s["judge"]:
        j = s["judge"]
        print(f"  LLM judge (0-2, separate metric): cites numbers {j['cites_numbers']:.2f}, "
              f"justified {j['justified']:.2f}  ({j['judged']} judged)")
    if s["failed_cases"]:
        print("  Cases with any failing attempt:", ", ".join(s["failed_cases"]))


def write_report(run_dir: Path, quiet: bool = False) -> dict:
    """Compute the summary from raw results, save summary.json, print the table."""
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    summary = summarize(load_rows(run_dir), meta)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if not quiet:
        print_summary(summary)
    return summary


def latest_run(variant: str | None = None) -> Path | None:
    from .run_eval import eval_dir
    runs = sorted(p for p in eval_dir().glob("*") if (p / "meta.json").exists()
                  and (variant is None or json.loads((p / "meta.json").read_text())["variant"] == variant))
    return runs[-1] if runs else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Report an eval run's metrics.")
    parser.add_argument("run_id", nargs="?", help="run folder name (default: latest)")
    args = parser.parse_args(argv)
    from .run_eval import eval_dir
    run_dir = eval_dir() / args.run_id if args.run_id else latest_run()
    if run_dir is None or not run_dir.exists():
        sys.exit("No eval runs yet. Run: python -m src.eval.run_eval --k 3")
    write_report(run_dir)


if __name__ == "__main__":
    main()
