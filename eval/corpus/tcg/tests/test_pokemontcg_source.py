"""Free price source (pokemontcg.io / TCGplayer) and per-source history.

PAYLOAD is the real response shape recorded from pokemontcg.io (Mew ex
232/091, 2026-10-15), so the mapping is tested against reality, offline.
"""

import pytest

from src import config
from src.decide import decide
from src.manage import add_card, list_cards, set_variant
from src.market.pokemontcg_source import PokemonTcgSource, choose_variant
from src.memory import append_snapshot, get_history
from src.market.types import MarketData

PAYLOAD = {"data": [{
    "id": "sv4pt5-232", "name": "Mew ex", "number": "232",
    "set": {"name": "Paldean Fates", "printedTotal": 91},
    "tcgplayer": {
        "url": "https://prices.pokemontcg.io/tcgplayer/sv4pt5-232",
        "updatedAt": "2026/10/15",
        "prices": {"holofoil": {"low": 729.99, "mid": 933.05, "high": 10000.0,
                                "market": 863.75, "directLow": 820.0}},
    },
}]}

MULTI_FINISH = {"data": [{
    "id": "base1-58", "name": "Pikachu", "number": "58",
    "set": {"printedTotal": 102},
    "tcgplayer": {"url": "https://example.test/p", "updatedAt": "2026/10/14", "prices": {
        "1stEditionNormal": {"low": 300.0, "market": 410.0},
        "normal": {"low": 5.0, "market": 9.5},
        "reverseHolofoil": {"low": 12.0, "market": 20.0, "directLow": 15.0},
    }},
}]}


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.ok = status, body, status == 200

    def json(self):
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((params, headers))
        return self.responses.pop(0)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)


def source(*responses):
    return PokemonTcgSource(session=Session(*responses), sleep=lambda s: None)


def test_maps_real_response(cache_dir):
    md = source(Resp(200, PAYLOAD)).fetch_market_data("Mew ex 232/091", "PAF", "Raw")
    assert md.sold_avg == 863.75                       # TCGplayer Market Price
    assert md.best_listing == 820.0                    # directLow (Near Mint), not low=729.99
    assert md.sold_count is None and md.sold_comps == []
    assert "Near Mint" in md.listings[0].condition
    assert md.source_note == "TCGplayer Holofoil prices via pokemontcg.io, updated 2026-10-15"


def test_falls_back_to_low_and_says_so(cache_dir):
    md = source(Resp(200, MULTI_FINISH)).fetch_market_data("Pikachu 58/102", "BS", "Raw", "normal")
    assert md.best_listing == 5.0 and "any condition" in md.listings[0].condition


def test_variant_selection():
    prices = MULTI_FINISH["data"][0]["tcgplayer"]["prices"]
    assert choose_variant(prices, None) == "normal"              # regular printing first
    assert choose_variant(prices, "reverseHolofoil") == "reverseHolofoil"
    assert choose_variant(prices, "holofoil") is None            # never silently swap finishes


def test_wanted_variant_missing_gives_no_data(cache_dir):
    md = source(Resp(200, PAYLOAD)).fetch_market_data("Mew ex 232/091", "PAF", "Raw", "reverseHolofoil")
    assert md.sold_avg is None


def test_graded_cards_are_skipped_without_a_call(cache_dir):
    s = source()
    md = s.fetch_market_data("Mew ex 232/091", "PAF", "PSA 10")
    assert md.sold_avg is None and "raw (ungraded)" in md.source_note
    assert s.session.calls == []


def test_no_price_yet_is_explained(cache_dir):
    # Real case: 30th Celebration Zapdos (me55-133) has a TCGplayer URL but no prices.
    zapdos = {"data": [{"id": "me55-133", "name": "Zapdos", "number": "133",
                        "set": {"printedTotal": 128}, "tcgplayer": {"url": "https://prices.pokemontcg.io/tcgplayer/me55-133"}}]}
    md = source(Resp(200, zapdos)).fetch_market_data("Zapdos 133/128", "30C", "Raw", catalog_id="me55-133")
    assert md.sold_avg is None and "no TCGplayer price for this card yet" in md.source_note

    md = source(Resp(200, PAYLOAD)).fetch_market_data("Mew ex 232/091", "PAF", "Raw", "reverseHolofoil")
    assert "no Reverse Holofoil price" in md.source_note and "Holofoil" in md.source_note


def test_wrong_printing_and_api_errors_degrade(cache_dir):
    # Same name & number but a different set size -> not our card.
    wrong = {"data": [{**PAYLOAD["data"][0], "set": {"printedTotal": 165}}]}
    assert source(Resp(200, wrong)).fetch_market_data("Mew ex 232/091", "PAF", "Raw").sold_avg is None
    # Three 500s in a row -> empty, not a crash.
    s = source(Resp(500), Resp(500), Resp(500))
    assert s.fetch_market_data("Mew ex 232/091", "PAF", "Raw").sold_avg is None


def test_prices_are_cached(cache_dir):
    s = source(Resp(200, PAYLOAD))
    s.fetch_market_data("Mew ex 232/091", "PAF", "Raw")
    again = PokemonTcgSource(session=Session(), sleep=lambda s: None)   # no responses queued
    assert again.fetch_market_data("Mew ex 232/091", "PAF", "Raw").sold_avg == 863.75


def test_optional_api_key_is_sent(cache_dir, monkeypatch):
    monkeypatch.setenv("POKEMONTCG_API_KEY", "pk-test-key-123")
    s = source(Resp(200, PAYLOAD))
    s.fetch_market_data("Mew ex 232/091", "PAF", "Raw")
    assert s.session.calls[0][1] == {"X-Api-Key": "pk-test-key-123"}


def test_unknown_sales_count_caps_confidence(seeded_db, cache_dir):
    card = next(c for c in list_cards() if c["name"] == "Mew ex 232/091")
    # 30% under market would be "high" with a known count >= 3...
    md = MarketData(best_listing=70.0, sold_avg=100.0, sold_count=None)
    d = decide(card, md, [])
    assert d.verdict == "buy" and d.confidence == "medium"
    assert "sales count not reported" in d.reason


# --- Per-source history ------------------------------------------------------- #

def test_history_is_kept_per_source(seeded_db):
    card_id = list_cards()[0]["id"]
    append_snapshot(card_id, MarketData(sold_avg=100.0))                       # mock (default)
    append_snapshot(card_id, MarketData(sold_avg=860.0), source="pokemontcg")
    assert [h["sold_avg"] for h in get_history(card_id)] == [100.0]
    assert [h["sold_avg"] for h in get_history(card_id, source="pokemontcg")] == [860.0]


def test_run_with_pokemontcg_uses_variant_and_tags_history(seeded_db, tmp_path, monkeypatch):
    from src.run import run
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    mew = next(c for c in list_cards() if c["name"] == "Mew ex 232/091")
    set_variant(mew["id"], "holofoil")
    seen = []

    def fake_fetch(self, name, set_code, grade, variant=None, catalog_id=None, condition=None):
        seen.append((name, variant))
        return MarketData(best_listing=820.0, sold_avg=863.75, sold_count=None) if name == mew["name"] else MarketData()

    monkeypatch.setattr(PokemonTcgSource, "fetch_market_data", fake_fetch)
    decisions = run("pokemontcg", llm_mode="off")
    assert ("Mew ex 232/091", "holofoil") in seen
    assert len(get_history(mew["id"], source="pokemontcg")) == 1
    assert get_history(mew["id"], source="mock") == []
    graded = next(d for d in decisions if d.grade == "PSA 10")
    assert graded.verdict == "hold" and graded.confidence == "low"          # no data -> hold/low


def test_variant_is_validated(seeded_db):
    with pytest.raises(ValueError, match="variant"):
        add_card("Pikachu 58/102", "BS", "Raw", "watching", variant="shiny")
