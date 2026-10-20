"""Add-card search: query building, result mapping, adding from the catalog."""

import json
import urllib.error
import urllib.request

import pytest

from src import catalog
from src.manage import list_cards
from src.market.pokemontcg_source import PokemonTcgSource

MEW = {
    "id": "sv4pt5-232", "name": "Mew ex", "number": "232", "rarity": "Special Illustration Rare",
    "set": {"id": "sv4pt5", "name": "Paldean Fates", "series": "Scarlet & Violet",
            "printedTotal": 91, "ptcgoCode": "PAF", "releaseDate": "2024/01/26"},
    "images": {"small": "https://images.pokemontcg.io/sv4pt5/232.png",
               "large": "https://images.pokemontcg.io/sv4pt5/232_hires.png"},
    "tcgplayer": {"url": "https://prices.pokemontcg.io/tcgplayer/sv4pt5-232", "updatedAt": "2026/10/15",
                  "prices": {"holofoil": {"low": 729.99, "market": 863.75, "directLow": 820.0}}},
}
BASE_PIKACHU = {
    "id": "base1-58", "name": "Pikachu", "number": "58", "rarity": "Common",
    "set": {"id": "base1", "name": "Base", "series": "Base", "printedTotal": 102,
            "ptcgoCode": "BS", "releaseDate": "1999/01/09"},
    "images": {"small": "https://images.pokemontcg.io/base1/58.png"},
    "tcgplayer": {"prices": {"normal": {"market": 9.5}, "1stEditionNormal": {"market": 410.0}}},
}
GALLERY = {
    "id": "swsh12pt5gg-GG01", "name": "Zeraora VSTAR", "number": "GG01",
    "set": {"id": "swsh12pt5gg", "name": "Crown Zenith Galarian Gallery", "printedTotal": 70,
            "releaseDate": "2023/01/20"},
}


# --- Query building ----------------------------------------------------------- #

@pytest.mark.parametrize("text, query", [
    ("charizard ex", "name:charizard* name:ex*"),
    ("pikachu 58", "name:pikachu* number:58"),
    ("#58", "number:58"),
    ("199/165", "number:199 set.printedTotal:165"),
    ("farfetch'd", "name:farfetch'd*"),
    ('mew" OR name:*', "name:mew* name:OR* name:name*"),   # quotes, colons, wildcards stripped
    ("charizard)(", "name:charizard*"),
])
def test_build_query(text, query):
    assert catalog.build_query(text) == query


def test_build_query_rejects_empty_after_cleaning():
    with pytest.raises(catalog.CatalogError):
        catalog.build_query('"" ::: ***')


def test_display_name():
    assert catalog.display_name(MEW) == "Mew ex 232/091"           # modern: 3-digit padding
    assert catalog.display_name(BASE_PIKACHU) == "Pikachu 58/102"  # vintage: as printed
    assert catalog.display_name(GALLERY) == "Zeraora VSTAR GG01"   # subset numbering
    # Some promo sets have no printedTotal at all (found in real search results).
    promo = {"name": "Pikachu", "number": "58", "set": {"id": "svp", "name": "SV Promos"}}
    assert catalog.display_name(promo) == "Pikachu 58"


def test_unexpected_server_error_is_json_500(server, monkeypatch):
    def broken(text):
        raise KeyError("boom")
    monkeypatch.setattr(catalog, "search", broken)
    status, body = get(server + "/api/search?q=mew")
    assert status == 500 and "data/run.log" in body["error"]


# --- Search -------------------------------------------------------------------- #

def fake_api(monkeypatch, responder):
    """Replace the pokemontcg.io client used by the catalog."""
    calls = []

    def fake_search(query, select, ttl=None, session=None, sleep=None, extra_params=None, **kw):
        calls.append((query, extra_params))
        return responder(query)

    monkeypatch.setattr(catalog, "api_search", fake_search)
    return calls


def test_search_maps_results_and_marks_cards_already_in_binder(seeded_db, monkeypatch):
    calls = fake_api(monkeypatch, lambda q: [MEW, BASE_PIKACHU])
    results = catalog.search("mew")
    assert calls[0] == ("name:mew*", {"orderBy": "-set.releaseDate", "pageSize": 30})
    mew, pika = results
    assert mew["display_name"] == "Mew ex 232/091" and mew["set"]["code"] == "PAF"
    assert mew["finishes"] == [{"key": "holofoil", "label": "Holofoil", "market": 863.75}]
    assert [x["status"] for x in mew["in_binder"]] == ["watching"]     # the seed has it
    assert pika["in_binder"] == []
    assert {f["key"] for f in pika["finishes"]} == {"normal", "1stEditionNormal"}


def test_search_caches_each_card_by_id(seeded_db, monkeypatch):
    from src import pokemontcg
    fake_api(monkeypatch, lambda q: [MEW])
    catalog.search("mew")
    # Adding it afterwards must not need the network: the real client finds it cached.
    class NoNetwork:
        def get(self, *a, **k):
            raise AssertionError("should have been served from cache")
    assert pokemontcg.search("id:sv4pt5-232", pokemontcg.CARD_FIELDS, session=NoNetwork()) == [MEW]


def test_search_errors(seeded_db, monkeypatch):
    with pytest.raises(catalog.CatalogError):
        catalog.search("a")
    fake_api(monkeypatch, lambda q: None)            # API down after retries
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.search("mew")


# --- Adding ---------------------------------------------------------------------- #

def by_id(q):
    return {"id:sv4pt5-232": [MEW], "id:base1-58": [BASE_PIKACHU]}.get(q, [])


def test_add_owned_card_from_catalog(seeded_db, monkeypatch):
    fake_api(monkeypatch, by_id)
    new_id = catalog.card_from_catalog("base1-58", {
        "status": "owned", "grade": "Raw", "variant": "1stEditionNormal",
        "purchase_price": "350", "purchase_date": "2025-06-01", "target_buy": "999",
    })
    card = next(c for c in list_cards() if c["id"] == new_id)
    assert card["name"] == "Pikachu 58/102" and card["set_code"] == "BS"
    assert card["catalog_id"] == "base1-58" and card["variant"] == "1stEditionNormal"
    assert card["purchase_price"] == 350.0 and card["target_buy"] is None   # buy target is watchlist-only
    assert card["image_url"] == "https://images.pokemontcg.io/base1/58.png"


def test_browser_supplied_names_are_ignored(seeded_db, monkeypatch):
    fake_api(monkeypatch, by_id)
    new_id = catalog.card_from_catalog("sv4pt5-232", {"status": "watching", "name": "<script>", "set_code": "X"})
    card = next(c for c in list_cards() if c["id"] == new_id)
    assert card["name"] == "Mew ex 232/091" and card["set_code"] == "PAF"


@pytest.mark.parametrize("form, field", [
    ({"status": "sold"}, "status"),
    ({"status": "owned", "grade": "Mint-ish"}, "grade"),
    ({"status": "owned", "variant": "reverseHolofoil"}, "variant"),          # not a finish of this card
    ({"status": "owned", "purchase_price": "-5"}, "purchase_price"),
    ({"status": "owned", "purchase_price": "lots"}, "purchase_price"),
    ({"status": "owned", "purchase_date": "2999-01-01"}, "purchase_date"),
    ({"status": "owned", "purchase_date": "yesterday"}, "purchase_date"),
])
def test_add_validation(seeded_db, monkeypatch, form, field):
    fake_api(monkeypatch, by_id)
    with pytest.raises(catalog.CatalogError) as info:
        catalog.card_from_catalog("sv4pt5-232", form)
    assert field in info.value.errors


def test_unknown_or_malformed_ids(seeded_db, monkeypatch):
    fake_api(monkeypatch, by_id)
    for bad in ("nope-1", "../../etc", 'x" OR id:*'):
        with pytest.raises(catalog.CatalogError):
            catalog.card_from_catalog(bad, {"status": "owned"})


def test_prices_use_catalog_id_when_present(tmp_path, monkeypatch):
    from src import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    seen = []

    def fake_search(query, select, ttl=None, session=None, sleep=None, **kw):
        seen.append(query)
        return [MEW]

    monkeypatch.setattr("src.market.pokemontcg_source.search", fake_search)
    md = PokemonTcgSource().fetch_market_data("anything", "PAF", "Raw", catalog_id="sv4pt5-232")
    assert seen == ["id:sv4pt5-232"] and md.sold_avg == 863.75


# --- HTTP routes ------------------------------------------------------------------- #

def post(url, payload, csrf=True):
    headers = {"Content-Type": "application/json", **({"X-Binder": "1"} if csrf else {})}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_search_and_add_over_http(server, monkeypatch):
    fake_api(monkeypatch, lambda q: [MEW] if q in ("name:mew*", "id:sv4pt5-232") else [])
    status, body = get(server + "/api/search?q=mew")
    assert status == 200 and body["results"][0]["id"] == "sv4pt5-232"

    assert post(server + "/api/cards", {"catalog_id": "sv4pt5-232", "status": "owned"}, csrf=False)[0] == 403
    status, body = post(server + "/api/cards", {"catalog_id": "sv4pt5-232", "status": "owned",
                                                "variant": "holofoil", "purchase_price": "800"})
    assert status == 201
    added = next(c for c in body["binder"]["cards"] if c["id"] == body["card_id"])
    assert added["name"] == "Mew ex 232/091" and added["status"] == "owned"

    status, body = post(server + "/api/cards", {"catalog_id": "sv4pt5-232", "status": "owned",
                                                "purchase_price": "-1"})
    assert status == 400 and "purchase_price" in body["errors"]


def test_search_unavailable_is_a_502(server, monkeypatch):
    fake_api(monkeypatch, lambda q: None)
    status, body = get(server + "/api/search?q=mew")
    assert status == 502 and "try again" in body["error"]
