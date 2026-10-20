"""Phases 7.2–7.4: suite loading, graders, and one test per failure-mode detector."""

import json

import pytest

from src.eval.cases import CATEGORIES, EvalCase, load_suite
from src.eval.detectors import (detect, detect_excessive_budget, detect_hallucinated_tool_args,
                                detect_loop, detect_schema_violation, detect_silent_abandonment)
from src.eval.graders import grade
from src.eval.trace import RunTrace, StepTrace

# --- 7.2 suite ------------------------------------------------------------------- #

def test_suite_loads_and_spans_every_category():
    cases = load_suite()
    assert 30 <= len(cases) <= 50
    assert {c.category for c in cases} == set(CATEGORIES)


def test_invalid_case_is_rejected(tmp_path):
    bad = {"task_id": "x", "category": "hold", "description": "no expectation", "card": {"name": "A"},
           "market": {}, "expected": {}}
    (tmp_path / "bad.json").write_text(json.dumps([bad]))
    with pytest.raises(ValueError, match="expected must set"):
        load_suite(tmp_path)
    good = {**bad, "expected": {"verdict": "hold"}}
    (tmp_path / "bad.json").write_text(json.dumps([good, good]))
    with pytest.raises(ValueError, match="duplicate"):
        load_suite(tmp_path)


# --- 7.3 graders ------------------------------------------------------------------ #

def case(**over):
    base = {"task_id": "c1", "category": "clear_buy", "description": "d",
            "card": {"name": "Charizard ex 199/165"},
            "market": {"best_listing": 412, "sold_avg": 598, "sold_count": 8},
            "expected": {"verdict": "buy"}}
    base.update(over)
    return EvalCase.model_validate(base)


def decision(**over):
    d = {"card_id": 1, "name": "Charizard ex 199/165", "grade": "Raw", "status": "watching",
         "verdict": "buy", "best_listing": 412.0, "sold_avg": 598.0, "sold_count": 8,
         "vs_market_pct": -31.1, "trend_30d": "flat", "confidence": "high",
         "reason": "Listing $412 is 31% under the $598 sold average.", "decided_by": "rules"}
    d.update(over)
    return d


def failing(g):
    return {r.grader for r in g.failures}


def test_correct_decision_passes_every_grader():
    g = grade(case(), decision())
    assert g.passed and not g.failures


def test_wrong_verdict_and_invented_numbers_fail():
    assert failing(grade(case(), decision(verdict="hold"))) == {"verdict"}
    assert "vs_market_math" in failing(grade(case(), decision(vs_market_pct=-40.0)))
    assert "vs_market_math" in failing(grade(case(), decision(sold_avg=650.0)))   # not the data's number


def test_confidence_cap_and_not_applicable():
    thin = case(expected={"confidence_max": "low"})
    assert failing(grade(thin, decision(confidence="medium"))) == {"confidence_cap"}
    g = grade(thin, decision(confidence="low"))
    assert g.passed and next(r for r in g.results if r.grader == "verdict").applicable is False


def test_pnl_rules_follow_the_project():
    owned = case(card={"name": "A", "status": "owned", "purchase_price": 180},
                 market={"sold_avg": 365, "sold_count": 7}, expected={"verdict": "sell"})
    ok = decision(status="owned", verdict="sell", best_listing=None, sold_avg=365.0, vs_market_pct=None,
                  unrealized_pnl=185.0, unrealized_pnl_pct=102.8)
    assert grade(owned, ok).passed
    assert "pnl_presence" in failing(grade(owned, {**ok, "unrealized_pnl_pct": None}))
    pulled = case(card={"name": "A", "status": "owned", "purchase_price": 0, "acquired": "pulled"},
                  market={"sold_avg": 54.2}, expected={"verdict": "hold"})
    pulled_ok = {**ok, "verdict": "hold", "sold_avg": 54.2, "sold_count": 0,
                 "unrealized_pnl": 54.2, "unrealized_pnl_pct": None}
    assert grade(pulled, pulled_ok).passed                                      # $0 cost: no %
    assert "pnl_presence" in failing(grade(pulled, {**pulled_ok, "unrealized_pnl_pct": 100.0}))
    assert "pnl_presence" in failing(grade(case(), decision(unrealized_pnl=5.0)))  # watched: none


def test_no_decision_fails_schema():
    assert {"schema", "verdict"} <= failing(grade(case(), None))


# --- 7.4 detectors: each fires on a bad trace, stays quiet on a clean one ---------- #

def step(i, type_, name=None, args=None, **kw):
    return StepTrace(step_index=i, type=type_, tool_name=name, tool_args=args,
                     timestamp="2026-10-20T00:00:00+00:00", **kw)


def clean_trace():
    return RunTrace(task_id="t", card={}, final_decision=decision(), total_tokens=2100, steps=[
        step(0, "tool_call", "fetch_market_data", {"name": "x"}),
        step(1, "llm_call", tokens_in=1900, tokens_out=200),
        step(2, "tool_call", "price_history", {"days": 90}),
        step(3, "llm_call"),
        step(4, "tool_call", "submit_decision", {"verdict": "buy", "confidence": "high", "reason": "r"}),
    ])


def test_clean_trace_triggers_nothing():
    assert not [d for d in detect(clean_trace()) if d.fired]


def test_hallucinated_tool_args():
    t = clean_trace()
    t.steps[2].tool_args = {"days": 45}                                    # not in the enum
    assert detect_hallucinated_tool_args(t).fired
    t = clean_trace()
    t.steps.append(step(5, "tool_call", "get_ebay_listings", {"q": "x"}))  # tool doesn't exist
    assert "unknown tool" in detect_hallucinated_tool_args(t).detail
    t = clean_trace()
    t.steps[2].tool_args = {"days": 90, "condition": "NM"}                 # extra argument
    assert detect_hallucinated_tool_args(t).fired


def test_loop():
    t = clean_trace()
    t.steps += [step(5 + i, "tool_call", "price_history", {"days": 90}) for i in range(2)]
    assert detect_loop(t).fired and "price_history x3" in detect_loop(t).detail


def test_silent_abandonment():
    t = clean_trace()
    t.final_decision = None
    assert detect_silent_abandonment(t).fired
    t = clean_trace()
    t.final_decision = {**decision(), "reason": "   "}
    assert detect_silent_abandonment(t).detail == "empty reason"


def test_schema_violation():
    t = clean_trace()
    t.final_decision = {**decision(), "verdict": "yolo"}
    assert detect_schema_violation(t).fired


def test_excessive_budget():
    t = clean_trace()
    t.steps += [step(5 + i, "llm_call") for i in range(10)]
    assert detect_excessive_budget(t).fired
    t = clean_trace()
    t.total_tokens = 50_000
    assert "tokens" in detect_excessive_budget(t).detail
