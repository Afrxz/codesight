"""T3.1 — snapshot writer + history reader."""

from src.manage import list_cards
from src.market.mock import MockSource
from src.memory import append_snapshot, get_history


def test_two_runs_give_two_rows_in_time_order(seeded_db):
    source = MockSource()
    cards = list_cards()

    # Simulate two runs a day apart (explicit timestamps keep the test deterministic,
    # but recent enough to fall inside the 30-day window).
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    t1 = (now - timedelta(days=1)).isoformat(timespec="seconds")
    t2 = now.isoformat(timespec="seconds")

    # Write the later run first to prove ordering comes from run_at, not insert order.
    for run_at in (t2, t1):
        for c in cards:
            append_snapshot(c["id"], source.fetch_market_data(c["name"], c["set_code"], c["grade"]), run_at)

    for c in cards:
        history = get_history(c["id"])
        assert len(history) == 2
        assert [h["run_at"] for h in history] == [t1, t2]


def test_history_window_excludes_old_rows(seeded_db):
    card_id = list_cards()[0]["id"]
    md = MockSource().fetch_market_data("Charizard ex 199/165", "151", "PSA 10")
    append_snapshot(card_id, md, "2020-01-01T00:00:00+00:00")  # far outside 30 days
    append_snapshot(card_id, md)                                # now
    assert len(get_history(card_id, days=30)) == 1
