"""JustTCG source, tested offline against responses recorded from the live API (2026-10)."""

import json
from pathlib import Path

import pytest

from src import config
from src.decide import compute_trend, decide
from src.market import justtcg
from src.market.justtcg import (JustTcgSource, choose_printing, normalize_set, pick_set,
                                same_number, to_market_data)
from src.market.types import MarketData

FIXTURES = Path(__file__).parent / "fixtures" / "justtcg"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


ZAPDOS_ID = "pokemon-me-30th-celebration-zapdos-133-128-illustration-rare"
CATALOG = {  # what pokemontcg.io knows about the card (cached when it was added)
    "me55-133": {"id": "me55-133", "name": "Zapdos", "number": "133",
                 "set": {"name": "30th Celebration", "printedTotal": 128}},
}


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.ok, self.headers = body, status, status == 200, {}

    def json(self):
        return self._body


class Session:
    """Answers like the real API from the recorded fixtures, and records calls."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        assert headers == {"x-api-key": "tcg_test"}
        path = url.replace(justtcg.BASE_URL, "")
        self.calls.append((method, path, params, json))
        if path == "/sets":
            return Resp(fixture("sets"))
        if method == "GET" and path == "/cards":
            return Resp(fixture("zapdos_in_set"))
        if method == "POST" and path == "/cards":
            wanted = {item["cardId"] for item in json}
            body = fixture("batch")
            body["data"] = [c for c in body["data"] if c["id"] in wanted]
            return Resp(body)
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(justtcg, "pokemontcg_search",
                        lambda q, select, ttl=None, **kw: [CATALOG[q.removeprefix("id:")]] if q.removeprefix("id:") in CATALOG else [])
    return JustTcgSource(api_key="tcg_test", session=Session(), sleep=lambda s: None)


# --- Pure helpers ------------------------------------------------------------------ #

def test_normalize_and_pick_set():
    assert normalize_set("ME: 30th Celebration Classic Collection") == "30th celebration classic collection"
    assert normalize_set("30th Celebration: Classic Collection") == "30th celebration classic collection"
    sets = fixture("sets")["data"]
    assert pick_set(sets, "30th Celebration")["id"] == "me-30th-celebration-pokemon"            # exact beats contains
    assert pick_set(sets, "30th Celebration: Classic Collection")["id"] == "me-30th-celebration-classic-collection-pokemon"
    assert pick_set(sets, "Evolving Skies") is None


def test_same_number():
    assert same_number("133/128", "133") and same_number("055/128", "55") and same_number("4/102", "4")
    assert same_number("TG05/TG30", "tg05")
    assert not same_number("133/128", "13") and not same_number("N/A", "4") and not same_number(None, "4")


def test_choose_printing():
    variants = [{"condition": "Near Mint", "printing": "Normal"},
                {"condition": "Near Mint", "printing": "Reverse Holofoil"},
                {"condition": "Lightly Played", "printing": "Holofoil"},
                {"condition": "Near Mint", "printing": "Holofoil", "language": "Japanese"}]
    assert choose_printing(variants, None) == "Normal"               # English Near Mint only
    assert choose_printing(variants, "reverseHolofoil") == "Reverse Holofoil"
    assert choose_printing(variants, "holofoil") is None              # never swap finishes


# --- Mapping the real recorded responses ----------------------------------------------- #

def test_maps_recorded_zapdos():
    card = next(c for c in fixture("batch")["data"] if c["id"] == ZAPDOS_ID)
    md = to_market_data(card, None)
    assert md.sold_avg == 5.17 and md.best_listing is None and md.sold_count is None
    assert md.trend_30d_pct == pytest.approx(-65.96)
    assert md.source_note.startswith("JustTCG Near Mint Holofoil price, updated 2026-10-")
    assert "-66.0% over 30 days" in md.source_note


def test_expired_price_is_not_used():
    card = {"variants": [{"condition": "Near Mint", "printing": "Holofoil", "price": None,
                          "priceStatus": "expired", "lastKnownPrice": 9.5, "lastUpdated": 1780000000}]}
    md = to_market_data(card, None)
    assert md.sold_avg is None and "expired" in md.source_note and "$9.50" in md.source_note


def test_missing_printing_is_explained():
    card = next(c for c in fixture("batch")["data"] if c["id"] == ZAPDOS_ID)
    md = to_market_data(card, "reverseHolofoil")
    assert md.sold_avg is None and "no Near Mint Reverse Holofoil price" in md.source_note


# --- Resolving ids and batching ------------------------------------------------------------ #

def test_resolve_finds_card_via_its_set_and_caches_the_id(source):
    md = source.fetch_market_data("Zapdos 133/128", "30C", "Raw", catalog_id="me55-133")
    assert md.sold_avg == 5.17
    calls = [(m, p) for m, p, *_ in source.session.calls]
    assert calls == [("GET", "/sets"), ("GET", "/cards")]        # search already carried the price
    assert source.session.calls[1][2]["set"] == "me-30th-celebration-pokemon"
    ids = json.loads((config.DATA_DIR / "cache" / "justtcg" / "ids.json").read_text())
    assert ids == {"me55-133": ZAPDOS_ID}

    again = JustTcgSource(api_key="tcg_test", session=Session(), sleep=lambda s: None)
    assert again.fetch_market_data("Zapdos 133/128", "30C", "Raw", catalog_id="me55-133").sold_avg == 5.17
    assert again.session.calls == []                              # id + price both cached


def test_set_search_uses_first_word_and_misses_are_not_cached(source):
    # Live finding: q="30th Celebration: Classic Collection" matches nothing; q="30th" finds both.
    assert source._set_id("30th Celebration: Classic Collection") == "me-30th-celebration-classic-collection-pokemon"
    assert source.session.calls[-1][2] == {"game": "pokemon", "q": "30th"}
    assert source._set_id("Evolving Skies") is None
    cached = json.loads((config.DATA_DIR / "cache" / "justtcg" / "sets.json").read_text())
    assert "Evolving Skies" not in cached          # retried next run, in case JustTCG adds it


def test_unreported_listings_are_not_sent_as_zero(seeded_db):
    from src.decide import build_facts
    from src.manage import list_cards
    card = list_cards()[0]
    assert build_facts(card, MarketData(sold_avg=5.17, sold_count=None), [])["listing_count"] is None
    from src.market.types import Listing
    with_listing = MarketData(sold_avg=5.0, best_listing=4.0, listings=[Listing(price=4.0, condition="NM")])
    assert build_facts(card, with_listing, [])["listing_count"] == 1


def test_prefetch_batches_twenty_cards_per_request(source):
    ids = {f"cat-{i}": f"jt-{i}" for i in range(25)}
    path = config.DATA_DIR / "cache" / "justtcg" / "ids.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids))
    cards = [{"name": f"Card {i}", "grade": "Raw", "catalog_id": f"cat-{i}"} for i in range(25)]
    source.prefetch(cards)
    posts = [c for c in source.session.calls if c[0] == "POST"]
    assert [len(c[3]) for c in posts] == [20, 5]
    assert posts[0][3][0] == {"cardId": "jt-0"}          # every condition comes back


def test_graded_and_unknown_cards(source):
    md = source.fetch_market_data("Zapdos 133/128", "30C", "PSA 10", catalog_id="me55-133")
    assert md.sold_avg is None and "not graded" in md.source_note and source.session.calls == []
    md = source.fetch_market_data("Mewtwo 999/128", "30C", "Raw")
    assert md.sold_avg is None and "Couldn't find this card" in md.source_note


def test_name_only_card_with_ambiguous_sets_asks_for_search(source, monkeypatch):
    # Same name and number in two sets, and no set known: refuse to guess.
    two_sets = {"data": [{"id": "a", "name": "Charizard", "number": "4/102", "set": "base-set"},
                         {"id": "b", "name": "Charizard", "number": "4/102", "set": "celebrations"}]}
    monkeypatch.setattr(source.session, "request", lambda *a, **k: Resp(two_sets))
    md = source.fetch_market_data("Charizard 4", "BS", "Raw")
    assert md.sold_avg is None and "+ Add card" in md.source_note


def test_missing_key_fails_loudly():
    with pytest.raises(config.ConfigError, match="JUSTTCG_API_KEY"):
        JustTcgSource()


# --- Decisions -------------------------------------------------------------------------------- #

def test_source_trend_used_before_history_exists():
    assert compute_trend([], MarketData(sold_avg=5.17, trend_30d_pct=-65.96)) == ("down", "source's 30-day change")
    assert compute_trend([], MarketData(sold_avg=5.0, trend_30d_pct=1.0))[0] == "flat"
    from datetime import datetime, timedelta, timezone
    ago = lambda days: (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    # Live finding: two runs on the same day must not override the source's 30-day trend.
    same_day = [{"sold_avg": 5.17, "run_at": ago(0.01)}]
    assert compute_trend(same_day, MarketData(sold_avg=5.17, trend_30d_pct=-65.96))[0] == "down"
    # Once our own history spans a week or more, it wins.
    week = [{"sold_avg": 4.0, "run_at": ago(10)}]
    assert compute_trend(week, MarketData(sold_avg=5.0, trend_30d_pct=-50.0)) == ("up", "history")
    # Sources without their own trend use history at any span (as before).
    assert compute_trend(same_day + [{"sold_avg": 6.0, "run_at": ago(0)}], MarketData(sold_avg=6.0))[0] == "up"


def test_run_with_justtcg(seeded_db, monkeypatch):
    from src.manage import add_card, list_cards
    from src.run import run
    monkeypatch.setenv("JUSTTCG_API_KEY", "tcg_test")
    monkeypatch.setattr(justtcg, "pokemontcg_search",
                        lambda q, select, ttl=None, **kw: [CATALOG[q.removeprefix("id:")]] if q.removeprefix("id:") in CATALOG else [])
    monkeypatch.setattr(justtcg.requests, "Session", Session)
    monkeypatch.setattr(justtcg, "MIN_INTERVAL_S", 0)
    card_id = add_card("Zapdos 133/128", "30C", "Raw", "owned", purchase_price=4.0, catalog_id="me55-133")
    decisions = {d.card_id: d for d in run("justtcg", llm_mode="off")}
    d = decisions[card_id]
    assert d.sold_avg == 5.17 and d.trend_30d == "down" and d.confidence in ("medium", "low")
    assert d.unrealized_pnl == 1.17


# --- Condition-aware pricing, condition table, history ------------------------------ #

CARD_WITH_CONDITIONS = {
    "id": "x", "tcgplayerId": "714372",
    "variants": [
        {"condition": "Near Mint", "printing": "Holofoil", "language": "English", "price": 157.37,
         "priceStatus": "current", "priceChange30d": -12.4, "lastUpdated": 1792434344,
         "priceHistory": [{"p": 206.66, "t": 1789516800}, {"p": 157.37, "t": 1792454400}]},
        {"condition": "Lightly Played", "printing": "Holofoil", "language": "English", "price": 141.16,
         "priceStatus": "current", "priceChange30d": 8.59, "lastUpdated": 1792434344},
        {"condition": "Damaged", "printing": "Holofoil", "language": "English", "price": 80.02,
         "priceStatus": "current", "lastUpdated": 1792434344},
    ],
}


def test_prices_the_copys_condition():
    md = to_market_data(CARD_WITH_CONDITIONS, None, "Lightly Played")
    assert md.sold_avg == 141.16 and "Lightly Played Holofoil" in md.source_note
    assert md.product_url == "https://www.tcgplayer.com/product/714372"
    assert [r["condition"] for r in md.by_condition] == ["Near Mint", "Lightly Played", "Damaged"]


def test_missing_condition_falls_back_to_near_mint_and_says_so():
    md = to_market_data(CARD_WITH_CONDITIONS, None, "Heavily Played")
    assert md.sold_avg == 157.37 and "No Heavily Played price" in md.source_note


def test_history_points():
    from src.market.justtcg import history_points
    points, label = history_points(CARD_WITH_CONDITIONS, None, None)
    assert [p["p"] for p in points] == [206.66, 157.37] and points[0]["t"].startswith("2026-09-16")
    assert label == "JustTCG · Near Mint Holofoil"


def test_price_history_route_uses_our_snapshots_without_justtcg(server):
    import urllib.request
    from src.manage import list_cards
    from src.memory import append_snapshot
    from datetime import datetime, timedelta, timezone
    cid = list_cards()[0]["id"]
    for days in (2, 1):
        append_snapshot(cid, MarketData(sold_avg=100.0 + days, best_listing=90.0), source="mock",
                        run_at=(datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds"))
    with urllib.request.urlopen(f"{server}/api/cards/{cid}/price-history?range=7d", timeout=10) as resp:
        body = json.loads(resp.read())
    assert body["source"] == "ours" and body["range"] == "7d"
    assert [p["p"] for p in body["points"]] == [102.0, 101.0] and body["points"][0]["b"] == 90.0
