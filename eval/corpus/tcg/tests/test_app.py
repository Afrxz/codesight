"""Phase 5 — dashboard renders both tabs from a mock run (headless AppTest)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src import config
from src.run import run

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def app_after_two_runs(seeded_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    run("mock", llm_mode="off")
    run("mock", llm_mode="off")       # 2 snapshots per card -> sparklines
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    return at


def test_watchlist_and_portfolio_tabs(app_after_two_runs):
    at = app_after_two_runs
    assert [t.label for t in at.tabs] == ["Watchlist", "Portfolio"]
    watch, port = (df.value for df in at.dataframe)
    assert len(watch) == 4 and len(port) == 3
    assert watch.iloc[0]["Verdict"] == "🟢 BUY"            # buys lead the watchlist
    assert port.iloc[0]["Verdict"] == "🔴 SELL"            # sells lead the portfolio
    assert port["P&L %"].notna().all()                     # P&L column populated


def test_summary_strip_matches_rows(app_after_two_runs):
    at = app_after_two_runs
    port = at.dataframe[1].value
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Collection value"] == f"${port['Sold avg'].sum():,.2f}"
    assert metrics["Unrealized P&L"] == f"+${port['P&L $'].sum():,.2f}"
    best = port.loc[port["P&L %"].idxmax()]
    assert metrics["Best: Charizard VMAX"] == f"{best['P&L %']:+.1f}%"
    assert best["Card"] == "Charizard VMAX 020/189"


def test_sparklines_after_two_snapshots(app_after_two_runs):
    for df in (d.value for d in app_after_two_runs.dataframe):
        # Cells round-trip through Arrow, so they come back as numpy arrays.
        assert all(v is not None and len(v) >= 2 for v in df["30-day trend"])


def test_empty_state_without_decisions(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert "No decisions yet" in at.info[0].value
