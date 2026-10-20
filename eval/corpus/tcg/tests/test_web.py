"""Binder UI backend: payload, HTTP routes, demo history, images, migration."""

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import date, timedelta

import pytest

from src import config
from src.db import init_db
from src.manage import add_card, list_cards
from src.market.mock import MockSource
from src.memory import get_history
from src.run import run
from src.web import build_binder


# --- Payload ---------------------------------------------------------------- #

def test_binder_payload_joins_cards_decisions_market_history(data_dir):
    run("mock", llm_mode="off")
    payload = build_binder()
    assert payload["source"] == "mock"
    assert len(payload["cards"]) == len(list_cards())
    for card in payload["cards"]:
        assert card["decision"]["card_id"] == card["id"]
        assert card["image_url"].startswith("https://images.pokemontcg.io/")
        assert card["market"]["listings"] and card["market"]["sold_comps"]
        assert len(card["history"]) == 1
    assert payload["summary"]["total_pnl"] == 280.0


def test_card_added_after_last_run_still_appears(data_dir):
    run("mock", llm_mode="off")
    new_id = add_card("Snorlax 051/165", "151", "Raw", "watching")
    card = next(c for c in build_binder()["cards"] if c["id"] == new_id)
    assert card["decision"] is None and card["market"] is None


def test_binder_before_any_run(data_dir):
    payload = build_binder()
    assert payload["generated_at"] is None
    assert all(c["decision"] is None for c in payload["cards"])


# --- HTTP server -------------------------------------------------------------- #

def fetch(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Content-Type"), err.read()


def test_serves_page_and_assets(server):
    status, ctype, body = fetch(server + "/")
    assert status == 200 and ctype.startswith("text/html") and b"Card Binder" in body
    assert fetch(server + "/binder.js")[0] == 200
    assert fetch(server + "/binder.css")[0] == 200


def test_only_whitelisted_files_are_served(server):
    for path in ("/../src/config.py", "/web/index.html", "/.env", "/%2e%2e/requirements.txt"):
        assert fetch(server + path)[0] == 404


def test_api_binder(server):
    status, ctype, body = fetch(server + "/api/binder")
    assert status == 200 and ctype == "application/json"
    assert len(json.loads(body)["cards"]) == 7


def test_run_requires_csrf_header(server):
    assert fetch(server + "/api/run", "POST")[0] == 403
    status, _, body = fetch(server + "/api/run", "POST", {"X-Binder": "1"})
    assert status == 200
    assert all(c["decision"] for c in json.loads(body)["cards"])


def test_binder_shows_the_last_ledger_run(server):
    assert json.loads(fetch(server + "/api/binder")[2])["agent"]["last_run"] is None
    body = json.loads(fetch(server + "/api/run", "POST", {"X-Binder": "1"})[2])
    last = body["agent"]["last_run"]
    assert last["status"] == "completed" and last["started_by"] == "binder"
    assert last["cards_done"] == last["cards_total"] == 7 and last["failed_cards"] == []


def test_run_rejects_unknown_source(server):
    assert fetch(server + "/api/run?source=evil", "POST", {"X-Binder": "1"})[0] == 400


def test_run_real_without_key_is_a_clear_400(server, monkeypatch):
    monkeypatch.delenv("TCGAPI_KEY", raising=False)
    status, _, body = fetch(server + "/api/run?source=real", "POST", {"X-Binder": "1"})
    assert status == 400 and b"TCGAPI_KEY" in body


# --- Removing cards -------------------------------------------------------------- #

def test_remove_card_deletes_card_and_history(data_dir):
    from src.manage import remove_card
    run("mock", llm_mode="off")
    card = list_cards()[0]
    assert remove_card(card["id"]) == (card["name"], 1)
    assert card["id"] not in {c["id"] for c in list_cards()}
    assert get_history(card["id"]) == []            # cascaded
    with pytest.raises(ValueError, match="no card"):
        remove_card(card["id"])


def test_delete_route(server):
    run("mock", llm_mode="off")
    owned = next(c for c in list_cards() if c["name"] == "Charizard VMAX 020/189")
    url = f"{server}/api/cards/{owned['id']}"

    assert fetch(url, "DELETE")[0] == 403                       # CSRF guard
    status, _, body = fetch(url, "DELETE", {"X-Binder": "1"})
    assert status == 200
    payload = json.loads(body)
    assert owned["id"] not in {c["id"] for c in payload["cards"]}
    # Summary no longer counts the removed card, even though decisions.json
    # (from the last run) still contains it: 280 - 185 = 95.
    assert payload["summary"]["total_pnl"] == 95.0 and payload["summary"]["card_count"] == 2

    assert fetch(url, "DELETE", {"X-Binder": "1"})[0] == 404    # already gone
    assert fetch(f"{server}/api/cards/abc", "DELETE", {"X-Binder": "1"})[0] == 404


# --- Demo history -------------------------------------------------------------- #

def test_mock_as_of_today_is_unchanged_and_past_drifts():
    today = MockSource().fetch_market_data("Charizard ex 199/165", "151", "PSA 10")
    assert MockSource(as_of=date.today()).fetch_market_data("Charizard ex 199/165", "151", "PSA 10") == today
    past = MockSource(as_of=date.today() - timedelta(days=20)).fetch_market_data(
        "Charizard ex 199/165", "151", "PSA 10")
    assert past.sold_avg < today.sold_avg     # Charizard ex has been climbing


def test_demo_backfills_history_and_trends(data_dir):
    from src.demo import build_demo
    decisions = build_demo(days=30, llm_mode="off")
    # 29 backfilled days (kept inside the 30-day window) + today's run.
    assert all(len(get_history(c["id"])) == 30 for c in list_cards())
    by_name = {d.name: d for d in decisions}
    assert by_name["Charizard ex 199/165"].trend_30d == "up"
    assert by_name["Umbreon VMAX 215/203"].trend_30d == "down"


# --- Images & migration ---------------------------------------------------------- #

class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = status == 200

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls += 1
        return self.responses.pop(0)


def test_find_image_matches_printed_total(data_dir):
    from src.images import find_image_url
    body = {"data": [
        {"number": "199", "set": {"printedTotal": 198}, "images": {"small": "wrong.png"}},
        {"number": "199", "set": {"printedTotal": 165}, "images": {"small": "right.png"}},
    ]}
    session = FakeSession(FakeResp(500, None), FakeResp(200, body))   # retries a 5xx
    assert find_image_url("Charizard ex 199/165", session, sleep=lambda s: None) == "right.png"
    # Cached: a second lookup makes no HTTP call.
    assert find_image_url("Charizard ex 199/165", FakeSession(), sleep=lambda s: None) == "right.png"


def test_old_database_gets_image_column(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:   # schema as it was before image_url existed
        conn.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, name TEXT, set_code TEXT, "
                     "grade TEXT, status TEXT, target_buy REAL, target_sell REAL, "
                     "purchase_price REAL, purchase_date TEXT, notes TEXT)")
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", db)
    init_db()
    init_db()   # idempotent
    with sqlite3.connect(db) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cards)")]
    conn.close()
    assert "image_url" in cols


def test_client_hanging_up_is_not_an_error(server, monkeypatch, caplog):
    """The search box cancels older searches; the server must treat that as normal."""
    import logging
    from src import web

    def hang_up(self, payload, status=200):
        raise ConnectionAbortedError("client went away")

    monkeypatch.setattr(web.BinderHandler, "_send_json", hang_up)
    with caplog.at_level(logging.DEBUG, logger="tcg.web"):
        try:
            fetch(server + "/api/binder")
        except Exception:
            pass                                   # the request gets no answer, by design
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Client disconnected" in r.getMessage() for r in caplog.records)
