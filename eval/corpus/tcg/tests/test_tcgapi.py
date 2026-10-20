"""T2.3 — live source mapping, grade matching, caching and resilience (offline).

A fake `requests.Session` stands in for the network so these tests are fast,
deterministic and never touch the real API.
"""

from datetime import date, timedelta

import pytest

from src.market.tcgapi import TcgApiSource, matches_grade, parse_grade

TODAY = date(2026, 10, 19)


def d(days_ago):
    return (TODAY - timedelta(days=days_ago)).isoformat()


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


class FakeSession:
    """Maps a URL path to a list of responses, served in order (last one repeats)."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        path = url.replace("https://api.tcgapi.net", "")
        self.calls.append((path, params, headers))
        queue = self.routes.get(path, [FakeResponse(404)])
        return queue.pop(0) if len(queue) > 1 else queue[0]


CARDS_BODY = {"data": [
    {"name": "Charizard ex", "number": "006/165", "set": "151",       # wrong printing
     "market_value": {"tcgplayer": 40.0}, "recent_sales": []},
    {"name": "Charizard ex", "number": "199/165", "set": "151",       # ours
     "market_value": {"tcgplayer": 120.0},
     "recent_sales": [{"source": "ebay", "price": 118.0, "date": d(3)},
                      {"source": "ebay", "price": 124.0, "date": d(10)}]},
]}

COMPS_BODY = {"comps": [
    {"title": "Charizard ex 199/165 PSA 10 Gem Mint", "price": "$610.00", "date": d(2)},
    {"title": "Charizard ex 199/165 PSA 10", "price": 590.0, "date": d(9)},
    {"title": "Charizard ex 199/165 PSA 9 Mint", "price": 260.0, "date": d(4)},    # wrong grade
    {"title": "Charizard ex 199/165 BGS 10 Pristine", "price": 900.0, "date": d(5)},  # wrong company
    {"title": "Charizard ex 199/165 Raw NM", "price": 120.0, "date": d(6)},       # raw
    {"title": "Charizard ex 006/165 PSA 10", "price": 80.0, "date": d(6)},        # other printing
    {"title": "Pokemon lot PSA 10 Charizard ex 199/165 x3", "price": 1500.0, "date": d(7)},  # junk
    {"title": "Charizard ex 199/165 PSA 10", "price": 400.0, "date": d(60)},      # too old
]}


def make_source(tmp_path, routes, **kw):
    return TcgApiSource(api_key="test-key", cache_dir=tmp_path / "cache",
                        session=FakeSession(routes), sleep=lambda s: None,
                        today=lambda: TODAY, **kw)


# --- Grade matching (pure) -------------------------------------------------- #

@pytest.mark.parametrize("target, title, expected", [
    ("PSA 10", "Charizard PSA 10 Gem Mint", True),
    ("PSA 10", "Charizard PSA 9", False),
    ("PSA 10", "Charizard BGS 10", False),
    ("PSA 10", "Charizard psa10", True),
    ("PSA 10", "Charizard ex NM", False),    # graded target, no grade info -> reject
    ("BGS 9.5", "Charizard BGS 9.5", True),
    ("Raw", "Charizard ex NM", True),
    ("Raw", "Charizard PSA 10", False),
    ("Raw", None, True),                      # card-level recent sale, no title
])
def test_matches_grade(target, title, expected):
    assert matches_grade(target, None, title) is expected


def test_parse_grade():
    assert parse_grade("CGC 9.5 Gem") == ("CGC", "9.5")
    assert parse_grade("Raw") is None


# --- Mapping onto MarketData ------------------------------------------------ #

def test_graded_card_uses_only_exact_grade_comps(tmp_path):
    src = make_source(tmp_path, {"/v1/cards": [FakeResponse(body=CARDS_BODY)],
                                 "/v1/comps": [FakeResponse(body=COMPS_BODY)]})
    md = src.fetch_market_data("Charizard ex 199/165", "151", "PSA 10")
    assert md.sold_count == 2                     # only the two in-window PSA 10s
    assert md.sold_avg == 600.0
    assert md.best_listing is None                # no raw proxy for a graded card
    # Auth header from config is sent.
    assert src.session.calls[0][2]["Authorization"] == "Bearer test-key"


def test_raw_card_falls_back_to_free_plan_data(tmp_path):
    # /v1/comps forbidden (not on the free plan) -> use card recent_sales.
    src = make_source(tmp_path, {"/v1/cards": [FakeResponse(body=CARDS_BODY)],
                                 "/v1/comps": [FakeResponse(403)]})
    md = src.fetch_market_data("Charizard ex 199/165", "151", "Raw")
    assert md.sold_count == 2 and md.sold_avg == 121.0
    assert md.best_listing == 120.0               # TCGplayer market price proxy
    assert md.listings[0].condition.startswith("Raw")


def test_unknown_card_degrades_to_empty(tmp_path):
    src = make_source(tmp_path, {"/v1/cards": [FakeResponse(body={"data": []})],
                                 "/v1/comps": [FakeResponse(body={"comps": []})]})
    md = src.fetch_market_data("Notacard xyz 999/999", "XXX", "PSA 10")
    assert md.best_listing is None and md.sold_avg is None and md.sold_count == 0


def test_unexpected_payload_never_raises(tmp_path):
    src = make_source(tmp_path, {"/v1/cards": [FakeResponse(body="garbage")],
                                 "/v1/comps": [FakeResponse(body={"comps": [{"price": None}]})]})
    md = src.fetch_market_data("Charizard ex 199/165", "151", "PSA 10")
    assert md.sold_count == 0


# --- Caching and resilience ------------------------------------------------- #

def test_responses_are_cached(tmp_path):
    routes = {"/v1/cards": [FakeResponse(body=CARDS_BODY)],
              "/v1/comps": [FakeResponse(body=COMPS_BODY)]}
    src = make_source(tmp_path, routes)
    first = src.fetch_market_data("Charizard ex 199/165", "151", "PSA 10")
    calls_after_first = src.live_calls
    second = src.fetch_market_data("Charizard ex 199/165", "151", "PSA 10")
    assert src.live_calls == calls_after_first == 2   # second fetch: zero HTTP calls
    assert first == second


def test_rate_limit_is_retried_with_backoff(tmp_path):
    slept = []
    src = TcgApiSource(api_key="k", cache_dir=tmp_path / "c", today=lambda: TODAY,
                       sleep=slept.append, min_interval_s=0,
                       session=FakeSession({
                           "/v1/cards": [FakeResponse(429, headers={"Retry-After": "7"}),
                                         FakeResponse(body=CARDS_BODY)],
                           "/v1/comps": [FakeResponse(503), FakeResponse(body=COMPS_BODY)],
                       }))
    md = src.fetch_market_data("Charizard ex 199/165", "151", "PSA 10")
    assert md.sold_count == 2
    assert 7.0 in slept                               # honoured Retry-After


def test_missing_key_fails_loudly(monkeypatch):
    from src.config import ConfigError
    monkeypatch.delenv("TCGAPI_KEY", raising=False)
    with pytest.raises(ConfigError, match="TCGAPI_KEY"):
        TcgApiSource()
