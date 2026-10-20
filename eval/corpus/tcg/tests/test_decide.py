"""T4.1 — decision function: verdicts, confidence, validation and repair."""

import json

import pytest

from src.decide import Decision, decide
from src.manage import list_cards
from src.market.mock import MockSource
from src.market.types import MarketData


def _decide_for(name, llm=None):
    card = next(c for c in list_cards() if c["name"] == name)
    market = MockSource().fetch_market_data(card["name"], card["set_code"], card["grade"])
    return decide(card, market, [], llm)


# --- Acceptance scenarios on MockSource (rule-based judgment) --------------- #

def test_under_market_card_is_buy(seeded_db):
    d = _decide_for("Charizard ex 199/165")
    assert d.verdict == "buy" and d.confidence == "high"
    assert d.vs_market_pct == -31.1
    assert d.trend_30d == "up"  # from the dated sold comps on a first run


def test_over_market_owned_card_is_sell(seeded_db):
    d = _decide_for("Charizard VMAX 020/189")
    assert d.verdict == "sell"
    assert d.unrealized_pnl == 185.0 and d.unrealized_pnl_pct == 102.8


def test_thin_data_card_has_low_confidence(seeded_db):
    d = _decide_for("Mew ex 232/091")
    assert d.sold_count <= 2 and d.confidence == "low"


def test_every_seeded_card_yields_a_valid_decision(seeded_db):
    for c in list_cards():
        d = _decide_for(c["name"])
        Decision.model_validate(d.model_dump())  # round-trips through the schema
        allowed = {"watching": {"buy", "hold"}, "owned": {"hold", "sell"}}[c["status"]]
        assert d.verdict in allowed


def test_no_market_data_holds_with_low_confidence(seeded_db):
    card = list_cards()[0]
    d = decide(card, MarketData(), [], llm=lambda s, u: pytest.fail("LLM must not be called"))
    assert d.verdict == "hold" and d.confidence == "low"


# --- LLM path: validation, repair, fallback, guardrails --------------------- #

class FakeLLM:
    """Returns scripted replies in order and records the prompts it received."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, system, user):
        self.prompts.append(user)
        return self.replies.pop(0)


GOOD = json.dumps({"verdict": "buy", "confidence": "high",
                   "reason": "Listing $412 is 31% under the $598 sold average."})


def test_llm_reply_is_used_and_labelled(seeded_db):
    d = _decide_for("Charizard ex 199/165", FakeLLM(GOOD))
    assert d.decided_by == "llm" and d.reason.startswith("Listing $412")
    # Numbers always come from code, never from the model.
    assert d.best_listing == 412.0 and d.sold_avg == 598.0


def test_llm_prompt_contains_computed_facts(seeded_db):
    llm = FakeLLM(GOOD)
    _decide_for("Charizard ex 199/165", llm)
    assert '"vs_market_pct": -31.1' in llm.prompts[0]


def test_fenced_json_is_repaired(seeded_db):
    d = _decide_for("Charizard ex 199/165", FakeLLM(f"Sure!\n```json\n{GOOD}\n```"))
    assert d.decided_by == "llm" and d.verdict == "buy"


def test_malformed_then_valid_retries_with_error(seeded_db):
    llm = FakeLLM("not json at all", GOOD)
    d = _decide_for("Charizard ex 199/165", llm)
    assert d.decided_by == "llm"
    assert "previous reply was invalid" in llm.prompts[1]


def test_disallowed_verdict_is_rejected(seeded_db):
    # "sell" on a watched card is invalid -> retry -> still invalid -> rules.
    bad = json.dumps({"verdict": "sell", "confidence": "high", "reason": "Sell it now please."})
    d = _decide_for("Charizard ex 199/165", FakeLLM(bad, bad))
    assert d.decided_by == "rules" and d.verdict == "buy"


def test_llm_exception_falls_back_to_rules(seeded_db):
    def broken(system, user):
        raise ConnectionError("network down")
    d = _decide_for("Charizard VMAX 020/189", broken)
    assert d.decided_by == "rules" and d.verdict == "sell"


def test_thin_data_confidence_is_capped_even_if_llm_says_high(seeded_db):
    reply = json.dumps({"verdict": "buy", "confidence": "high",
                        "reason": "Listing $95 is under the $110 sold avg."})
    d = _decide_for("Mew ex 232/091", FakeLLM(reply))
    assert d.decided_by == "llm" and d.confidence == "low"
