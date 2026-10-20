"""Phase 7.1 — trace schema + instrumentation (must not change behavior)."""

import json

import pytest

from src import config
from src.decide import AgentContext, decide
from src.eval import trace
from src.eval.trace import RunTrace, traced_decision
from src.market.mock import MockSource
from src.market.types import MarketData
from src.tools import Toolbox

CARD = {"id": 1, "name": "Charizard ex 199/165", "set_code": "151", "grade": "PSA 10",
        "status": "watching", "target_buy": 420.0, "target_sell": 650.0, "purchase_price": None,
        "purchase_date": None, "notes": None, "image_url": None, "variant": None, "catalog_id": None,
        "manual_price": None, "manual_price_date": None, "acquired": None, "condition": None}


def scripted_chat(*replies):
    replies = list(replies)
    return lambda messages, tools: replies.pop(0)


def call(name, args, id_="c"):
    return {"id": id_, "name": name, "arguments": json.dumps(args)}


SUBMIT = call("submit_decision", {"verdict": "buy", "confidence": "high",
                                  "reason": "Listing $412 is 31% under the $598 sold average."}, "s")


def agent_with(*replies):
    return AgentContext(chat=scripted_chat(*replies),
                        toolbox=lambda c, m: Toolbox(card=c, market=m, source_name="mock"), budget=3)


def test_trace_captures_fetch_tool_and_submit_steps():
    decision, t = traced_decision("t1", CARD, MockSource(), [],
                                  agent=agent_with({"content": None, "tool_calls": [call("price_history", {"days": 30})]},
                                                   {"content": None, "tool_calls": [SUBMIT]}))
    RunTrace.model_validate(t.model_dump())
    names = [(s.type, s.tool_name) for s in t.steps]
    assert names[0] == ("tool_call", "fetch_market_data")
    assert ("tool_call", "price_history") in names and ("tool_call", "submit_decision") in names
    assert t.steps[0].tool_result["sold_avg"] == 598.0 and t.steps[0].latency_ms >= 0
    assert t.final_decision["verdict"] == "buy" and decision.verdict == "buy"


def test_tracing_does_not_change_the_decision():
    """Same inputs, with and without a tracer -> identical Decision."""
    market = MockSource().fetch_market_data(CARD["name"], CARD["set_code"], CARD["grade"])
    plain = decide(CARD, market, [], agent=agent_with({"content": None, "tool_calls": [SUBMIT]}))
    traced, _ = traced_decision("t2", CARD, MockSource(), [],
                                agent=agent_with({"content": None, "tool_calls": [SUBMIT]}))
    assert plain.model_dump() == traced.model_dump()
    rules_plain = decide(CARD, market, [])
    rules_traced, _ = traced_decision("t3", CARD, MockSource(), [])
    assert rules_plain.model_dump() == rules_traced.model_dump()


def test_no_tracer_means_no_overhead_and_no_state():
    assert trace.current() is None
    decide(CARD, MarketData(sold_avg=598, best_listing=412, sold_count=5), [])
    assert trace.current() is None


def test_failing_step_is_recorded_and_reraised():
    class Broken(MockSource):
        def fetch_market_data(self, *a, **k):
            raise ConnectionError("source down")

    decision, t = traced_decision("t4", CARD, Broken(), [])
    assert decision is None and t.final_decision is None            # abandoned, visibly
    assert t.steps[0].tool_name == "fetch_market_data" and "ConnectionError" in t.steps[0].error
    with trace.tracing("t5", CARD) as tracer:                         # the hook re-raises
        with pytest.raises(ConnectionError):
            with tracer.tool_step("x", {}):
                raise ConnectionError("boom")


def test_cost_only_from_configured_prices():
    with trace.tracing("t6", CARD) as tracer:
        tracer.llm_call(tokens_in=1_000_000, tokens_out=500_000, latency_ms=10)
        assert tracer.finish(None).total_cost_usd is None             # no prices -> unknown, not $0
    config.save({"LLM_PRICE_IN_PER_M": "0.5", "LLM_PRICE_OUT_PER_M": "2"})
    with trace.tracing("t7", CARD) as tracer:
        tracer.llm_call(tokens_in=1_000_000, tokens_out=500_000, latency_ms=10)
        t = tracer.finish(None)
    assert t.steps[0].cost_usd == 1.5 and t.total_cost_usd == 1.5 and t.total_tokens == 1_500_000
