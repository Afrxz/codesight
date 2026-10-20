"""T4.2 — end-to-end run loop on MockSource."""

import json

from src.memory import get_history
from src.manage import list_cards
from src.run import run


def test_run_writes_a_decision_per_card(seeded_db, tmp_path):
    out = tmp_path / "decisions.json"
    decisions = run("mock", llm_mode="off", out_path=out)

    cards = list_cards()
    assert len(decisions) == len(cards)
    payload = json.loads(out.read_text())
    assert payload["source"] == "mock"
    assert {d["card_id"] for d in payload["decisions"]} == {c["id"] for c in cards}

    # Each run leaves one snapshot per card in memory.
    run("mock", llm_mode="off", out_path=out)
    assert all(len(get_history(c["id"])) == 2 for c in cards)


def test_pnl_only_on_owned_cards(seeded_db, tmp_path):
    for d in run("mock", llm_mode="off", out_path=tmp_path / "d.json"):
        if d.status == "owned":
            assert d.unrealized_pnl is not None and d.unrealized_pnl_pct is not None
        else:
            assert d.unrealized_pnl is None and d.unrealized_pnl_pct is None


def test_one_failing_card_does_not_abort_the_run(seeded_db, tmp_path, monkeypatch):
    from src.market.mock import MockSource
    original = MockSource.fetch_market_data

    def flaky(self, name, set_code, grade, variant=None, catalog_id=None, condition=None):
        if name.startswith("Pikachu"):
            raise RuntimeError("boom")
        return original(self, name, set_code, grade, variant, catalog_id, condition)

    monkeypatch.setattr(MockSource, "fetch_market_data", flaky)
    decisions = run("mock", llm_mode="off", out_path=tmp_path / "d.json")
    assert len(decisions) == len(list_cards()) - 1
