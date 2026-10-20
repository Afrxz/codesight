"""Phase 7.8 — "only buy on comparable evidence" (facts + guardrail), offline."""

import json
from datetime import date, timedelta

from src.decide import comparable_evidence, condition_rank, decide, is_comparable_listing
from src.market.types import Listing, MarketData, SoldComp


def card(grade="Raw", condition=None, status="watching", target_buy=None):
    return {"id": 1, "name": "Charizard 4/102", "set_code": "BS", "grade": grade, "condition": condition,
            "status": status, "purchase_price": None, "target_buy": target_buy, "target_sell": None,
            "acquired": None}


def market(listings, sales, sold_avg=None):
    today = date(2026, 10, 18)
    return MarketData(listings=[Listing(price=p, condition=c) for p, c in listings],
                      sold_comps=[SoldComp(price=p, date=today - timedelta(days=3 * i)) for i, p in enumerate(sales)],
                      sold_avg=sold_avg, sold_count=len(sales))


def test_condition_rank_reads_names_and_abbreviations():
    assert condition_rank("Raw · Near Mint") == 0 and condition_rank("Raw/NM") == 0
    assert condition_rank("Raw · Heavily Played") == 3 and condition_rank("Damaged (creased)") == 4
    assert condition_rank("Raw (TCGplayer market)") is None


def test_comparable_listing_rules():
    assert is_comparable_listing("Raw · Near Mint", "Raw", "Near Mint")
    assert not is_comparable_listing("Raw · Damaged", "Raw", "Near Mint")
    assert is_comparable_listing("Raw · Near Mint", "Raw", "Lightly Played")      # a better copy is fine
    assert not is_comparable_listing("Raw · Holofoil (TCGplayer low, any condition)", "Raw", "Near Mint")
    assert not is_comparable_listing("PSA 10", "Raw", "Near Mint")                 # a slab isn't a raw copy
    assert is_comparable_listing("Raw (TCGplayer market)", "Raw", None)            # names no condition
    assert is_comparable_listing("PSA 10", "PSA 10", None)
    assert not is_comparable_listing("PSA 10", "PSA 1", None)                      # not a substring match
    assert not is_comparable_listing("PSA 9", "PSA 10", None)


def test_clean_market_has_no_comparable_evidence():
    m = market([(150, "Raw · Near Mint")], [200, 198, 202, 199, 201])
    assert comparable_evidence(card(), m) is None


def test_damaged_cheapest_listing_is_held_with_the_reason():
    m = market([(85, "Raw · Damaged"), (194, "Raw · Near Mint")], [196, 204, 200, 198, 202])
    d = decide(card(), m, [])
    assert d.verdict == "hold" and d.guardrails == ["comparable_evidence"]
    assert "Damaged" in d.reason and "$194" in d.reason
    # The shown numbers stay as the data reports them.
    assert d.best_listing == 85 and d.vs_market_pct == -57.5


def test_outlier_inflated_average_is_held():
    m = market([(104, "Raw · Near Mint")], [98, 102, 100, 99, 600])
    d = decide(card(), m, [])
    assert d.verdict == "hold" and "outlier" in d.reason and "$100" in d.reason


def test_real_comparable_edge_is_still_a_buy():
    # Cheapest is damaged, but the Near Mint copy is itself 25% under: a genuine buy.
    m = market([(60, "Raw · Damaged"), (150, "Raw · Near Mint")], [200, 198, 202, 199, 201])
    d = decide(card(), m, [])
    assert d.verdict == "buy" and d.guardrails == []


def test_guardrail_overrides_the_llm_and_keeps_its_view():
    m = market([(85, "Raw · Damaged"), (194, "Raw · Near Mint")], [196, 204, 200, 198, 202])
    reply = json.dumps({"verdict": "buy", "confidence": "high", "reason": "Listing is 57% under the average."})
    prompts = []
    d = decide(card(), m, [], llm=lambda s, u: prompts.append(u) or reply)
    assert d.verdict == "hold" and d.decided_by == "llm"
    assert "The model's view: Listing is 57% under" in d.reason
    assert '"cheapest_comparable_listing": 194.0' in prompts[0]      # the model saw the evidence
