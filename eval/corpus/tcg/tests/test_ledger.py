"""Phases 8.1–8.3, 8.5 — run ledger, resume, retry/fail-continue, scheduled runs (mock, offline)."""

import threading
from datetime import datetime

import pytest
import requests

from src import ledger, run as run_mod
from src.db import get_connection
from src.manage import list_cards
from src.market.base import TransientSourceError
from src.market.mock import MockSource
from src.run import run


@pytest.fixture(autouse=True)
def no_backoff_wait(monkeypatch):
    waits = []
    monkeypatch.setattr(run_mod, "_backoff_sleep", waits.append)
    return waits


def snapshot_count(run_id=None):
    with get_connection() as conn:
        if run_id:
            return conn.execute("SELECT COUNT(*) FROM price_history WHERE run_id = ?", (run_id,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]


def progress(run_id):
    with get_connection() as conn:
        return {r["card_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM run_progress WHERE run_id = ?", (run_id,))}


# --- 8.1 ledger ---------------------------------------------------------------

def test_completed_run_leaves_a_complete_ledger(data_dir):
    decisions = run("mock", "off", out_path=data_dir / "d.json")
    [r] = ledger.recent_runs()
    cards = list_cards()
    assert r["status"] == "completed" and r["finished_at"]
    assert r["cards_total"] == r["cards_done"] == len(cards) == len(decisions)
    states = progress(r["run_id"])
    assert set(states) == {c["id"] for c in cards}
    assert all(p["state"] == "done" and p["decision_json"] and p["market_json"] for p in states.values())
    assert snapshot_count(r["run_id"]) == len(cards)


def test_checkpoint_is_written_after_each_card_not_at_the_end(data_dir, monkeypatch):
    seen = []
    original = ledger.checkpoint_done

    def spy(run_id, card_id, *args, **kwargs):
        original(run_id, card_id, *args, **kwargs)
        seen.append(ledger.get_run(run_id)["cards_done"])     # visible in the DB right away
    monkeypatch.setattr(ledger, "checkpoint_done", spy)
    run("mock", "off", out_path=data_dir / "d.json")
    assert seen == list(range(1, len(list_cards()) + 1))


# --- 8.2 resume -----------------------------------------------------------------

def interrupt_after(n, monkeypatch):
    """Make the run die (like a kill) right after its n-th checkpoint.

    Returns a function that puts the real checkpoint back (never use
    monkeypatch.undo() here: it would also undo the fixtures' temp-DB patches).
    """
    original, count = ledger.checkpoint_done, {"n": 0}

    def dying(*args, **kwargs):
        original(*args, **kwargs)
        count["n"] += 1
        if count["n"] == n:
            raise KeyboardInterrupt     # BaseException: skips the run's own error handling, like a kill
    monkeypatch.setattr(ledger, "checkpoint_done", dying)
    return lambda: monkeypatch.setattr(ledger, "checkpoint_done", original)


def test_resume_processes_only_the_remaining_cards(data_dir, monkeypatch):
    cards = list_cards()
    restore = interrupt_after(3, monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        run("mock", "off", out_path=data_dir / "d.json")
    interrupted = ledger.find_interrupted()
    assert interrupted["cards_done"] == 3 and snapshot_count() == 3
    restore()

    fetched = []
    original = MockSource.fetch_market_data
    monkeypatch.setattr(MockSource, "fetch_market_data",
                        lambda self, name, *a, **k: fetched.append(name) or original(self, name, *a, **k))
    decisions = run("mock", "off", out_path=data_dir / "d.json")

    assert fetched == [c["name"] for c in cards[3:]]                # done cards skipped
    assert len(decisions) == len(cards)                            # but included in the result
    [r] = ledger.recent_runs()                                     # same run, not a new one
    assert r["run_id"] == interrupted["run_id"] and r["status"] == "completed"
    assert snapshot_count() == len(cards)                          # one per card, no duplicates
    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == len(cards)


def test_duplicate_snapshot_is_refused_by_the_database(data_dir):
    from src.market.types import MarketData
    from src.memory import append_snapshot
    card_id = list_cards()[0]["id"]
    assert append_snapshot(card_id, MarketData(sold_avg=1.0), source="mock", run_id="r1") is not None
    assert append_snapshot(card_id, MarketData(sold_avg=2.0), source="mock", run_id="r1") is None
    assert snapshot_count("r1") == 1


def test_interrupted_run_on_another_source_is_superseded(data_dir, monkeypatch):
    stale = ledger.start_run(list_cards(), "justtcg", "off")
    run("mock", "off", out_path=data_dir / "d.json")
    old = ledger.get_run(stale)
    assert old["status"] == "failed" and "superseded" in old["error"]
    assert ledger.find_interrupted() is None


# --- 8.3 retry + fail-continue -----------------------------------------------------

def test_transient_failure_is_retried_with_backoff(data_dir, monkeypatch, no_backoff_wait):
    original, calls = MockSource.fetch_market_data, {"n": 0}

    def flaky_once(self, name, *a, **k):
        if name.startswith("Pikachu") and calls["n"] < 2:
            calls["n"] += 1
            raise requests.ConnectionError("connection reset")
        return original(self, name, *a, **k)
    monkeypatch.setattr(MockSource, "fetch_market_data", flaky_once)
    run("mock", "off", out_path=data_dir / "d.json")

    [r] = ledger.recent_runs()
    assert r["cards_failed"] == 0 and r["cards_done"] == len(list_cards())
    pikachu = next(c for c in list_cards() if c["name"].startswith("Pikachu"))
    assert progress(r["run_id"])[pikachu["id"]]["attempts"] == 3
    assert no_backoff_wait == [1.0, 2.0]                            # exponential backoff


def test_card_that_keeps_failing_is_marked_failed_and_the_run_continues(data_dir, monkeypatch):
    original = MockSource.fetch_market_data

    def down_for_pikachu(self, name, *a, **k):
        if name.startswith("Pikachu"):
            raise TransientSourceError("upstream 503")
        return original(self, name, *a, **k)
    monkeypatch.setattr(MockSource, "fetch_market_data", down_for_pikachu)
    decisions = run("mock", "off", out_path=data_dir / "d.json")

    cards = list_cards()
    [r] = ledger.recent_runs()
    assert r["status"] == "completed" and len(decisions) == len(cards) - 1
    assert r["cards_done"] == len(cards) - 1 and r["cards_failed"] == 1
    [failed] = r["failed_cards"]
    assert failed["name"].startswith("Pikachu") and failed["attempts"] == 3
    assert "TransientSourceError: upstream 503" in failed["error"]


def test_a_bug_is_not_retried(data_dir, monkeypatch, no_backoff_wait):
    original = MockSource.fetch_market_data

    def buggy(self, name, *a, **k):
        if name.startswith("Pikachu"):
            raise KeyError("price")
        return original(self, name, *a, **k)
    monkeypatch.setattr(MockSource, "fetch_market_data", buggy)
    run("mock", "off", out_path=data_dir / "d.json")
    [r] = ledger.recent_runs()
    assert r["failed_cards"][0]["attempts"] == 1 and no_backoff_wait == []


def test_every_card_failing_fails_the_run(data_dir, monkeypatch):
    def down(self, *a, **k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(MockSource, "fetch_market_data", down)
    assert run("mock", "off", out_path=data_dir / "d.json") == []
    [r] = ledger.recent_runs()
    assert r["status"] == "failed" and "Every card failed" in r["error"]


# --- 8.5 scheduled runs --------------------------------------------------------------

def test_two_scheduled_firings_survive_a_restart(data_dir, monkeypatch):
    from src import config, scheduler
    config.save({"AUTO_RUN": "on", "RUN_TIME": "09:00"})
    fire = lambda: run("mock", "off", out_path=data_dir / "d.json", started_by="schedule")
    lock = threading.Lock()

    day1 = scheduler.DailyScheduler(fire, lock, clock=lambda: datetime(2026, 10, 20, 9, 30))
    assert day1.tick() is True
    # "Restart": a new scheduler (fresh process state), same day -> doesn't run again.
    restarted = scheduler.DailyScheduler(fire, lock, clock=lambda: datetime(2026, 10, 20, 18, 0))
    assert restarted.tick() is False
    restarted.clock = lambda: datetime(2026, 10, 21, 9, 5)
    assert restarted.tick() is True

    runs = ledger.recent_runs()
    assert len(runs) == 2 and all(r["status"] == "completed" and r["started_by"] == "schedule" for r in runs)
    with get_connection() as conn:
        per_card = conn.execute("SELECT card_id, COUNT(*) n FROM price_history GROUP BY card_id").fetchall()
    assert len(per_card) == len(list_cards()) and all(r["n"] == 2 for r in per_card)


def test_startup_resumes_an_interrupted_run(data_dir, monkeypatch):
    from src import config, scheduler
    config.save({"RUN_SOURCE": "mock", "RUN_LLM": "off"})
    restore = interrupt_after(2, monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        run(out_path=config.DATA_DIR / "decisions.json")
    restore()
    assert scheduler.resume_interrupted_run(threading.Lock()) is True
    [r] = ledger.recent_runs()
    assert r["status"] == "completed" and r["cards_done"] == len(list_cards())
    assert scheduler.resume_interrupted_run(threading.Lock()) is False    # nothing left to resume
