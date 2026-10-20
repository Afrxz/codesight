"""Phase 7.7 — regression harness: diff a run against a named baseline.

    python -m src.eval.regress --set-baseline llm 20261020-081500-llm-baseline
    python -m src.eval.regress --baseline llm                 # latest run vs baseline "llm"
    python -m src.eval.regress --baseline llm --run <run_id>  # a specific run

Every run is tagged with a `variant` label (the prompt / model / code version
it tested). Baselines are named pointers to runs, kept in
data/eval/baselines.json. A diff shows each metric's baseline value, new value,
change, and whether that's improved / regressed / unchanged — and exits with
code 1 if anything regressed beyond its tolerance, so it can gate a change.

Each metric knows which direction is better and how much noise to tolerate:
rates have an absolute tolerance (2 points); cost/latency/size metrics a
relative one (25%), since those wobble between runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .detectors import DETECTOR_NAMES

RATE_TOL = 0.02          # absolute, for 0-1 rates
RELATIVE_TOL = 0.25      # relative, for cost / latency / steps / tokens

# metric -> (higher_is_better, tolerance_kind)
METRICS: dict[str, tuple[bool, str]] = {
    "success_rate": (True, "rate"),
    "pass_at_k": (True, "rate"),
    "pass_all_k": (True, "rate"),
    "tool_call_failure_rate": (False, "rate"),
    "llm_fallback_rate": (False, "rate"),
    "avg_cost_usd": (False, "relative"),
    "avg_latency_ms": (False, "relative"),
    "avg_steps": (False, "relative"),
    "avg_tokens": (False, "relative"),
    **{f"failure_modes.{name}": (False, "rate") for name in DETECTOR_NAMES},
}


def _baselines_path() -> Path:
    from .run_eval import eval_dir
    return eval_dir() / "baselines.json"


def load_baselines() -> dict:
    path = _baselines_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def set_baseline(name: str, run_id: str) -> None:
    from .run_eval import eval_dir
    if not (eval_dir() / run_id / "summary.json").exists():
        raise SystemExit(f"No summary for run {run_id!r}")
    baselines = load_baselines()
    baselines[name] = run_id
    _baselines_path().write_text(json.dumps(baselines, indent=2), encoding="utf-8")


def _get(summary: dict, metric: str):
    if metric.startswith("failure_modes."):
        return summary["failure_modes"].get(metric.split(".", 1)[1])
    if metric.startswith("by_category."):
        return summary["by_category"].get(metric.split(".", 1)[1])
    return summary.get(metric)


def compare(baseline: dict, run: dict) -> list[dict]:
    """Per-metric diff rows: metric, baseline, run, delta, status."""
    metrics = dict(METRICS)
    for cat in sorted(set(baseline["by_category"]) | set(run["by_category"])):
        metrics[f"by_category.{cat}"] = (True, "rate")
    rows = []
    for metric, (higher_better, kind) in metrics.items():
        old, new = _get(baseline, metric), _get(run, metric)
        if old is None or new is None:
            rows.append({"metric": metric, "baseline": old, "run": new, "delta": None, "status": "n/a"})
            continue
        delta = new - old
        tolerance = RATE_TOL if kind == "rate" else abs(old) * RELATIVE_TOL
        if abs(delta) <= tolerance:
            status = "same"
        else:
            status = "improved" if (delta > 0) == higher_better else "regressed"
        rows.append({"metric": metric, "baseline": old, "run": new, "delta": round(delta, 6), "status": status})
    return rows


def print_diff(rows: list[dict], baseline_id: str, run_id: str) -> None:
    def fmt(metric, v):
        if v is None:
            return "n/a"
        if "rate" in metric or metric.startswith(("by_category", "failure_modes", "pass")):
            return f"{v:.1%}"
        return f"{v:,.4f}" if metric == "avg_cost_usd" else f"{v:,.1f}"

    print(f"\nRegression diff: {run_id}\n          vs baseline: {baseline_id}")
    print(f"  {'metric':40} {'baseline':>10} {'run':>10}  status")
    print("  " + "-" * 72)
    marks = {"improved": "▲ improved", "regressed": "▼ REGRESSED", "same": "  same", "n/a": "  n/a"}
    for r in rows:
        print(f"  {r['metric']:40} {fmt(r['metric'], r['baseline']):>10} {fmt(r['metric'], r['run']):>10}  "
              f"{marks[r['status']]}")
    counts = {s: sum(r["status"] == s for r in rows) for s in ("improved", "regressed", "same")}
    print(f"\n  {counts['improved']} improved, {counts['regressed']} regressed, {counts['same']} unchanged")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Diff an eval run against a named baseline.")
    parser.add_argument("--set-baseline", nargs=2, metavar=("NAME", "RUN_ID"),
                        help="name a run as a baseline")
    parser.add_argument("--baseline", help="baseline name (see --set-baseline)")
    parser.add_argument("--run", help="run id to compare (default: the latest run)")
    args = parser.parse_args(argv)

    from .report import latest_run
    from .run_eval import eval_dir
    if args.set_baseline:
        set_baseline(*args.set_baseline)
        print(f"Baseline {args.set_baseline[0]!r} -> {args.set_baseline[1]}")
        return
    if not args.baseline:
        parser.error("give --baseline NAME (or --set-baseline NAME RUN_ID)")
    baselines = load_baselines()
    if args.baseline not in baselines:
        sys.exit(f"No baseline named {args.baseline!r}. Known: {', '.join(baselines) or 'none'}")
    base_id = baselines[args.baseline]
    run_dir = eval_dir() / args.run if args.run else latest_run()
    base = json.loads((eval_dir() / base_id / "summary.json").read_text(encoding="utf-8"))
    run = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    rows = compare(base, run)
    print_diff(rows, base_id, run_dir.name)
    sys.exit(1 if any(r["status"] == "regressed" for r in rows) else 0)


if __name__ == "__main__":
    main()
