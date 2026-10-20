"""Streamlit dashboard: Watchlist and Portfolio views over the agent's output.

Reads `data/decisions.json` (written by `python -m src.run`) and the
`price_history` table for sparklines. The dashboard is read-only — it shows
recommendations and their reasons; it never takes any action.

    streamlit run app.py
"""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from src import config
from src.db import STATUS_OWNED, STATUS_WATCHING, init_db
from src.cardnames import base_name
from src.memory import get_history
from src.pnl import portfolio_summary

# Verdicts carry an icon AND a word, so meaning never depends on color alone.
VERDICT_BADGE = {"buy": "🟢 BUY", "hold": "🟡 HOLD", "sell": "🔴 SELL"}
CONFIDENCE_LABEL = {"high": "High", "medium": "Medium", "low": "Low ⚠"}


# --- Data loading -------------------------------------------------------------- #

def load_run() -> dict | None:
    path = config.DATA_DIR / "decisions.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def sparkline(card_id: int, days: int = 30) -> list[float] | None:
    """Sold-average series for the trend column; None until there are 2+ points.

    Reads only the latest run's source, so sample and real prices never mix.
    """
    source = (load_run() or {}).get("source") or "mock"
    history = get_history(card_id, days, source=source)
    points = [h["sold_avg"] for h in history if h["sold_avg"] is not None]
    return points if len(points) >= 2 else None


# Column order per view: lead with what that view is for. The watchlist is about
# the buying edge; the portfolio is about cost basis and P&L.
WATCH_COLUMNS = ["Card", "Grade", "Verdict", "Confidence", "Best listing",
                 "Sold avg", "vs market", "Sales", "30-day trend"]
OWNED_COLUMNS = ["Card", "Grade", "Verdict", "Confidence", "Paid", "Sold avg",
                 "P&L $", "P&L %", "30-day trend", "Best listing", "vs market", "Sales"]


def build_table(decisions: list[dict], owned: bool) -> pd.DataFrame:
    rows = [{
        "Card": d["name"],
        "Grade": d["grade"],
        "Verdict": VERDICT_BADGE[d["verdict"]],
        "Confidence": CONFIDENCE_LABEL[d["confidence"]],
        "Best listing": d["best_listing"],
        "Sold avg": d["sold_avg"],
        "Sales": d["sold_count"],
        "vs market": d["vs_market_pct"],
        "30-day trend": sparkline(d["card_id"]),
        "Paid": d["purchase_price"],
        "P&L $": d["unrealized_pnl"],
        "P&L %": d["unrealized_pnl_pct"],
    } for d in decisions]
    return pd.DataFrame(rows, columns=OWNED_COLUMNS if owned else WATCH_COLUMNS)


def signed_usd(v: float) -> str:
    """+$280.00 / -$12.50 (sign before the dollar sign)."""
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


COLUMN_CONFIG = {
    "Best listing": st.column_config.NumberColumn(format="$%.2f"),
    "Sold avg": st.column_config.NumberColumn(format="$%.2f",
                                              help="Average of recent sold comps"),
    "Sales": st.column_config.NumberColumn(help="Sold comps behind the average"),
    "vs market": st.column_config.NumberColumn(
        format="%+.1f%%", help="Cheapest listing vs sold average (negative = under market)"),
    "30-day trend": st.column_config.LineChartColumn(
        "30-day trend", help="Sold average per run, last 30 days (needs 2+ runs)"),
    "Paid": st.column_config.NumberColumn(format="$%.2f"),
    "P&L $": st.column_config.NumberColumn(format="%+.2f", help="Sold avg − price paid, in $"),
    "P&L %": st.column_config.NumberColumn(format="%+.1f%%"),
}


# --- Rendering ------------------------------------------------------------------ #

def render_reasons(decisions: list[dict]) -> None:
    st.markdown("##### Why")
    for d in decisions:
        with st.expander(f"{VERDICT_BADGE[d['verdict']]} · {d['name']} ({d['grade']})"):
            st.write(d["reason"])
            st.caption(
                f"Confidence: {d['confidence']} · trend: {d['trend_30d']} · "
                f"{d['sold_count']} sales · decided by: {d['decided_by']}"
            )


def render_watchlist(decisions: list[dict]) -> None:
    watching = [d for d in decisions if d["status"] == STATUS_WATCHING]
    if not watching:
        st.info("No watched cards. Add one with `python -m src.manage add ... --status watching`.")
        return
    # Buys first: on the watchlist, opportunities are the headline.
    order = {"buy": 0, "hold": 1, "sell": 2}
    watching.sort(key=lambda d: (order[d["verdict"]], d["vs_market_pct"] or 0))
    st.dataframe(build_table(watching, owned=False), column_config=COLUMN_CONFIG,
                 hide_index=True, width="stretch")
    render_reasons(watching)


def render_portfolio(decisions: list[dict]) -> None:
    owned = [d for d in decisions if d["status"] == STATUS_OWNED]
    if not owned:
        st.info("No owned cards yet. Record one with `python -m src.manage set-purchase ...`.")
        return

    s = portfolio_summary(owned)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Collection value", f"${s['total_value']:,.2f}",
              help="Sum of sold averages (what these cards actually sell for)")
    c2.metric("Unrealized P&L", signed_usd(s["total_pnl"]),
              delta=f"{s['total_pnl_pct']:+.1f}%" if s["total_pnl_pct"] is not None else None,
              help=f"Against a total cost basis of ${s['total_cost']:,.2f}")
    if s["best"]:
        # Value = the % (short, never truncates); the card name goes in the label.
        for col, title, d in ((c3, "Best", s["best"]), (c4, "Worst", s["worst"])):
            col.metric(f"{title}: {base_name(d['name'])}", f"{d['unrealized_pnl_pct']:+.1f}%",
                       help=f"{d['name']} ({d['grade']}): {signed_usd(d['unrealized_pnl'])}")

    # Sells first: on the portfolio, "time to take profit" is the headline.
    order = {"sell": 0, "hold": 1, "buy": 2}
    owned.sort(key=lambda d: (order[d["verdict"]], -(d["unrealized_pnl_pct"] or 0)))
    st.dataframe(build_table(owned, owned=True), column_config=COLUMN_CONFIG,
                 hide_index=True, width="stretch")
    render_reasons(owned)


def main() -> None:
    st.set_page_config(page_title="TCG Market Agent", page_icon="🃏", layout="wide")
    init_db()

    st.title("TCG Market Agent")
    st.caption("Pokémon card buy / hold / sell recommendations with reasons. "
               "**Decision support only, not investment advice.** You make the call.")

    run = load_run()
    if run is None:
        st.info("No decisions yet. Run the agent first:\n\n"
                "```\npython -m src.run --source mock\n```")
        st.stop()

    decisions = run["decisions"]
    generated = datetime.fromisoformat(run["generated_at"]).strftime("%Y-%m-%d %H:%M UTC")
    rules = sum(d["decided_by"] == "rules" for d in decisions)
    note = f" · {rules} decided by rules (no LLM)" if rules else ""
    st.caption(f"Last run: {generated} · source: **{run['source']}** · "
               f"{len(decisions)} cards{note}")

    tab_watch, tab_port = st.tabs(["Watchlist", "Portfolio"])
    with tab_watch:
        render_watchlist(decisions)
    with tab_port:
        render_portfolio(decisions)


main()
