"""Phases 7.5–7.7: runner, report and regression diff (rules engine; offline)."""

import json

from src.eval.cases import load_suite
from src.eval.regress import compare, load_baselines, set_baseline
from src.eval.report import load_rows, summarize, write_report
from src.eval.run_eval import pinned_settings, run_eval


def small_suite():
    """Four real cases, one with its expectation flipped so a failure is guaranteed."""
    cases = [c for c in load_suite() if c.task_id in ("buy-01", "outlier-01", "thin-01", "hold-06")]
    wrong = next(c for c in cases if c.task_id == "outlier-01")
    cases[cases.index(wrong)] = wrong.model_copy(
        update={"expected": wrong.expected.model_copy(update={"verdict": "buy"})})
    return cases


def test_runner_writes_per_attempt_results(data_dir):
    out = run_eval(k=2, variant="t", cases=small_suite())
    rows = load_rows(out)
    assert len(rows) == 8 and {r["attempt"] for r in rows} == {1, 2}
    row = rows[0]
    assert {"grades", "detections", "trace", "passed", "llm_fallback"} <= set(row)
    assert row["trace"]["steps"][0]["tool_name"] == "fetch_market_data"
    meta = json.loads((out / "meta.json").read_text())
    assert meta["k"] == 2 and meta["pinned_settings"]["BUY_EDGE_PCT"] == 15.0 and meta["prompt_sha"]


def test_report_metrics(data_dir):
    out = run_eval(k=2, variant="t", cases=small_suite())
    s = write_report(out, quiet=True)
    assert s["attempts"] == 8 and s["cases"] == 4
    assert s["success_rate"] == 0.75                  # the flipped outlier-01 fails
    assert s["pass_at_k"] == 0.75 and s["pass_all_k"] == 0.75
    assert s["failed_cases"] == ["outlier-01"] and s["grader_failures"] == {"verdict": 2}
    assert s["tool_call_failure_rate"] == 0.0 and s["tool_calls"] == 8
    assert summarize(load_rows(out), json.loads((out / "meta.json").read_text())) == s


def test_pinned_settings_ignore_your_own(data_dir):
    from src import config
    config.save({"STRONG_EDGE_PCT": "60", "BUY_EDGE_PCT": "50"})
    with pinned_settings() as pinned:
        assert pinned["BUY_EDGE_PCT"] == 15.0 and config.get("BUY_EDGE_PCT") == 15.0
    assert config.get("BUY_EDGE_PCT") == 50.0                 # restored afterwards


def test_regression_diff(data_dir):
    base = {"success_rate": 0.75, "pass_at_k": 0.8, "pass_all_k": 0.7, "tool_call_failure_rate": 0.10,
            "llm_fallback_rate": 0.0, "avg_cost_usd": None, "avg_latency_ms": 1000.0, "avg_steps": 4.0,
            "avg_tokens": 3000.0, "by_category": {"clear_buy": 1.0, "outlier_comps": 0.0},
            "failure_modes": {"loop": 0.0, "hallucinated_tool_args": 0.05, "silent_abandonment": 0.0,
                              "schema_violation": 0.0, "excessive_budget": 0.0}}
    new = json.loads(json.dumps(base))
    new.update(success_rate=0.9, tool_call_failure_rate=0.11, avg_tokens=4500.0)
    new["by_category"]["outlier_comps"] = 0.8
    new["failure_modes"]["hallucinated_tool_args"] = 0.2
    status = {r["metric"]: r["status"] for r in compare(base, new)}
    assert status["success_rate"] == "improved"
    assert status["tool_call_failure_rate"] == "same"            # within the 2-point tolerance
    assert status["avg_tokens"] == "regressed"                   # +50% > 25%
    assert status["by_category.outlier_comps"] == "improved"
    assert status["failure_modes.hallucinated_tool_args"] == "regressed"
    assert status["avg_cost_usd"] == "n/a"


def test_named_baselines(data_dir):
    out = run_eval(k=1, variant="b", cases=small_suite())
    write_report(out, quiet=True)
    set_baseline("main", out.name)
    assert load_baselines() == {"main": out.name}
