"""Your own price: a fallback when no market source can price a card."""

import json
import urllib.error
import urllib.request

import pytest

from src import config
from src.decide import decide
from src.manage import list_cards, set_manual_price
from src.market.types import MarketData
from src.run import refresh_card, run, with_manual_price


def card(name="Charizard VMAX 020/189"):
    return next(c for c in list_cards() if c["name"] == name)


def test_fallback_only_when_source_has_no_price(seeded_db):
    c = card()
    set_manual_price(c["id"], 300)
    c = card()
    empty = MarketData(source_note="pokemontcg.io has no TCGplayer price for this card yet.")
    md = with_manual_price(c, empty)
    assert md.sold_avg == 300.0 and md.manual and md.sold_count is None
    assert "your own price" in md.source_note and "no TCGplayer price" in md.source_note
    # Market data always wins over your price.
    real = MarketData(sold_avg=365.0, sold_count=7)
    assert with_manual_price(c, real) is real


def test_manual_price_gives_pnl_but_low_confidence(seeded_db):
    set_manual_price(card()["id"], 400)          # owned, paid 180, sell target 350
    c = card()
    d = decide(c, with_manual_price(c, MarketData()), [])
    assert d.unrealized_pnl == 220.0 and d.verdict == "sell"
    assert d.confidence == "low" and d.reason.startswith("Based on your own price of $400")


def test_no_price_reason_explains_why(seeded_db):
    d = decide(card(), MarketData(source_note="pokemontcg.io has no TCGplayer price for this card yet."), [])
    assert d.verdict == "hold" and d.confidence == "low"
    assert "no TCGplayer price for this card yet" in d.reason and "set your own price" in d.reason


def test_run_uses_fallback_but_history_stays_honest(seeded_db, monkeypatch):
    from src.market.mock import MockSource
    from src.memory import get_history
    monkeypatch.setattr(MockSource, "fetch_market_data",
                        lambda self, *a, **k: MarketData(source_note="nothing"))
    set_manual_price(card()["id"], 300)
    d = next(x for x in run("mock", llm_mode="off") if x.name == "Charizard VMAX 020/189")
    assert d.sold_avg == 300.0
    # The snapshot records what the market said (nothing), not your estimate.
    assert get_history(card()["id"])[-1]["sold_avg"] is None


def test_set_manual_price_validation(seeded_db):
    with pytest.raises(ValueError):
        set_manual_price(card()["id"], -1)
    with pytest.raises(ValueError, match="no card"):
        set_manual_price(999, 5)
    set_manual_price(card()["id"], 12.345)
    assert card()["manual_price"] == 12.35 and card()["manual_price_date"]
    set_manual_price(card()["id"], None)
    assert card()["manual_price"] is None and card()["manual_price_date"] is None


def test_refresh_card_updates_only_that_card(seeded_db):
    run("mock", llm_mode="off")
    path = config.DATA_DIR / "decisions.json"
    before = json.loads(path.read_text())
    target = card("Lugia V 186/195")
    set_manual_price(target["id"], 999)            # ignored: mock has a market price for Lugia
    refresh_card(target["id"])
    after = json.loads(path.read_text())
    assert after["generated_at"] == before["generated_at"]
    assert len(after["decisions"]) == len(before["decisions"])
    others = lambda p: sorted((d for d in p["decisions"] if d["card_id"] != target["id"]), key=lambda d: d["card_id"])
    assert others(after) == others(before)


# --- HTTP ---------------------------------------------------------------------- #

def post(url, payload, csrf=True):
    headers = {"Content-Type": "application/json", **({"X-Binder": "1"} if csrf else {})}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_price_route(server, monkeypatch):
    from src.market.mock import MockSource
    monkeypatch.setattr(MockSource, "fetch_market_data", lambda self, *a, **k: MarketData(source_note="none"))
    cid = card()["id"]
    url = f"{server}/api/cards/{cid}/price"
    assert post(url, {"price": 300}, csrf=False)[0] == 403
    status, body = post(url, {"price": 300})
    assert status == 200 and body["refreshed"] is True
    c = next(x for x in body["binder"]["cards"] if x["id"] == cid)
    assert c["manual_price"] == 300.0
    assert c["decision"]["sold_avg"] == 300.0 and c["decision"]["confidence"] == "low"
    assert c["market"]["manual"] is True

    assert post(url, {"price": "abc"})[0] == 400
    assert post(f"{server}/api/cards/999/price", {"price": 5})[0] == 404
    status, body = post(url, {"price": None})
    assert status == 200 and next(x for x in body["binder"]["cards"] if x["id"] == cid)["manual_price"] is None


def test_justtcg_check_key_messages():
    from src.market.justtcg import check_key

    class Session:
        def __init__(self, status, body=None):
            self.status, self.body = status, body

        def get(self, url, headers=None, timeout=None):
            assert headers == {"x-api-key": "tcg_test"}
            resp = type("R", (), {})()
            resp.status_code, resp.ok = self.status, self.status == 200
            resp.json = lambda: self.body
            return resp

    assert check_key("tcg_test", Session(401))[0] is False
    ok, msg = check_key("tcg_test", Session(200, {"data": [], "_metadata": {"apiPlan": "Free", "apiRequestsRemaining": 98}}))
    assert ok and "Plan: Free" in msg and "98" in msg
