"""Edit card (PATCH /api/cards/<id>) and the new setting types."""

import json
import urllib.error
import urllib.request

import pytest

from src import config
from src.catalog import CatalogError, update_from_form
from src.manage import list_cards, update_card


def card(name="Mew ex 232/091"):
    return next(c for c in list_cards() if c["name"] == name)


def test_edit_moves_card_to_collection_with_purchase_info(seeded_db):
    cid = card()["id"]
    update_from_form(cid, {"status": "owned", "grade": "Raw", "variant": "holofoil",
                           "purchase_price": "820", "purchase_date": "2026-10-01", "target_buy": "999"})
    c = card()
    assert c["status"] == "owned" and c["purchase_price"] == 820.0 and c["variant"] == "holofoil"
    assert c["target_buy"] is None                       # buy targets are watchlist-only


@pytest.mark.parametrize("form, field", [
    ({"status": "owned", "purchase_price": "-3"}, "purchase_price"),
    ({"status": "owned", "variant": "shiny"}, "variant"),
    ({"status": "gone"}, "status"),
])
def test_edit_validation(seeded_db, form, field):
    with pytest.raises(CatalogError) as info:
        update_from_form(card()["id"], form)
    assert field in info.value.errors


def test_update_card_whitelist(seeded_db):
    with pytest.raises(ValueError, match="not editable"):
        update_card(card()["id"], {"name": "Hacked"})


def patch(url, payload, csrf=True):
    headers = {"Content-Type": "application/json", **({"X-Binder": "1"} if csrf else {})}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_patch_route(server):
    cid = card()["id"]
    url = f"{server}/api/cards/{cid}"
    assert patch(url, {"status": "owned"}, csrf=False)[0] == 403
    status, body = patch(url, {"status": "owned", "grade": "Raw", "purchase_price": "100"})
    assert status == 200 and body["refreshed"] is True
    c = next(x for x in body["binder"]["cards"] if x["id"] == cid)
    assert c["purchase_price"] == 100.0 and c["decision"]["unrealized_pnl"] == 10.0
    assert "agent" in body["binder"] and "schedule" in body["binder"]["agent"]
    assert patch(url, {"status": "owned", "purchase_price": "x"})[0] == 400
    assert patch(f"{server}/api/cards/999", {"status": "owned"})[0] == 404


@pytest.mark.parametrize("key, value, ok", [
    ("RUN_TIME", "09:30", True), ("RUN_TIME", "9:30", False), ("RUN_TIME", "25:00", False),
    ("ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc", True),
    ("ALERT_WEBHOOK_URL", "http://example.com/hook", False),
    ("SEARXNG_URL", "http://localhost:8080", True),
    ("AGENT_TOOL_BUDGET", "7", False),
])
def test_agent_settings_validation(key, value, ok):
    if ok:
        assert config.validate(key, value)
    else:
        with pytest.raises(config.ConfigError):
            config.validate(key, value)


def test_webhook_url_is_masked(seeded_db):
    config.save({"ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/1/secretTOKEN99"})
    item = next(i for i in config.describe() if i["key"] == "ALERT_WEBHOOK_URL")
    assert item["is_set"] and "secretTOKEN" not in json.dumps(config.describe())


# --- Cards pulled from packs ------------------------------------------------------ #

def test_pulled_card_has_zero_cost_and_full_value_gain(seeded_db):
    from src.decide import decide
    from src.market.types import MarketData
    from src.pnl import compute_pnl, portfolio_summary
    cid = card()["id"]
    update_from_form(cid, {"status": "owned", "grade": "Raw", "acquired": "pulled", "purchase_price": ""})
    c = card()
    assert c["acquired"] == "pulled" and c["purchase_price"] == 0.0          # blank pack cost -> $0

    assert compute_pnl("owned", 0.0, 5.17) == (5.17, None)                    # % undefined at $0 cost
    assert compute_pnl("owned", 4.0, 5.17) == (1.17, 29.2)                    # a counted pack cost works normally

    d = decide(c, MarketData(sold_avg=110.0, sold_count=None), [])
    assert d.unrealized_pnl == 110.0 and d.unrealized_pnl_pct is None
    assert "pulled from a pack at no cost, so the full value is gain" in d.reason
    assert "record what was paid" not in d.reason.lower()

    summary = portfolio_summary([d.model_dump()])
    assert summary["cards_with_pnl"] == 1 and summary["total_pnl"] == 110.0
    assert summary["total_pnl_pct"] is None and summary["best"] is None      # no % ranking at $0 cost


def test_acquired_is_validated(seeded_db):
    with pytest.raises(CatalogError) as info:
        update_from_form(card()["id"], {"status": "owned", "acquired": "stolen"})
    assert "acquired" in info.value.errors
    with pytest.raises(ValueError):
        update_card(card()["id"], {"acquired": "stolen"})


def test_pulled_cards_are_not_flagged_as_missing_a_price(seeded_db):
    from src.decide import decide
    from src.market.types import MarketData
    from src.portfolio import collection_facts
    cid = card()["id"]
    update_from_form(cid, {"status": "owned", "grade": "Raw", "acquired": "pulled"})
    d = decide(card(), MarketData(sold_avg=110.0, sold_count=None), [])
    assert card()["name"] not in collection_facts([d], list_cards())["missing_purchase_price"]


def test_condition_for_raw_cards_only(seeded_db):
    cid = card()["id"]                                   # Mew ex, raw
    update_from_form(cid, {"status": "watching", "grade": "Raw", "condition": "Lightly Played"})
    assert card()["condition"] == "Lightly Played"
    update_from_form(cid, {"status": "watching", "grade": "Raw", "condition": "Near Mint"})
    assert card()["condition"] is None                   # Near Mint is the default: stored as NULL
    update_from_form(cid, {"status": "watching", "grade": "PSA 10", "condition": "Damaged"})
    assert card()["condition"] is None                   # a graded card's condition is its grade
    with pytest.raises(CatalogError) as info:
        update_from_form(cid, {"status": "watching", "grade": "Raw", "condition": "Mint-ish"})
    assert "condition" in info.value.errors
