"""The agent grades its own past calls.

Every run records each recommendation (verdict, confidence, and the market
price it was based on). Later, we look up what the price actually did 7 and
30 days afterwards, from the same source's price history, and score the call:

    buy   right if the price rose by >= MOVE_PCT, wrong if it fell by >= MOVE_PCT
    sell  right if the price fell by >= MOVE_PCT (you avoided the drop), wrong if it rose
    hold  right if the price stayed within HOLD_BAND_PCT, wrong if it moved more
          (holding through a big move means a buy or sell was missed)
    anything in between is "neutral" and doesn't count either way

hit rate = right / (right + wrong). It's broken down by verdict, confidence and
whether the LLM or the rules made the call. Nothing is stored: grades are
recomputed from the two tables each time, so changing a threshold re-grades
everything consistently.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .db import get_connection

if TYPE_CHECKING:
    from .decide import Decision

HORIZONS = (7, 30)       # days after the call
MOVE_PCT = 5.0           # a move this big counts as "the price went somewhere"
HOLD_BAND_PCT = 10.0     # a hold is right if the price stays within +/- this
MIN_FOR_PROMPT = 3       # graded calls needed before the track record is shown to the LLM


def record(decisions: list["Decision"], source: str, made_at: str | None = None,
           markets: dict | None = None, conn=None) -> None:
    """Store this run's recommendations. Calls based on your own price are kept
    without a market price (there's nothing to grade them against).

    `conn`: write inside the caller's transaction (the run ledger records each
    card's call together with its checkpoint, so a resumed run can't repeat it)."""
    made_at = made_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for d in decisions:
        manual = bool(markets and markets.get(d.card_id) is not None and markets[d.card_id].manual)
        rows.append((d.card_id, made_at, source, d.verdict, d.confidence,
                     None if manual else d.sold_avg, d.decided_by, d.reason))
    sql = ("INSERT INTO recommendations (card_id, made_at, source, verdict, confidence, price, "
           "decided_by, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
    if conn is not None:
        conn.executemany(sql, rows)
        return
    with get_connection() as own:
        own.executemany(sql, rows)


def outcome(verdict: str, change_pct: float) -> str:
    """'right' | 'wrong' | 'neutral' for one call, given the later price change."""
    if verdict == "buy":
        return "right" if change_pct >= MOVE_PCT else "wrong" if change_pct <= -MOVE_PCT else "neutral"
    if verdict == "sell":
        return "right" if change_pct <= -MOVE_PCT else "wrong" if change_pct >= MOVE_PCT else "neutral"
    return "right" if abs(change_pct) < HOLD_BAND_PCT else "wrong"


def _later_price(conn, card_id: int, source: str, after: datetime) -> float | None:
    """First priced snapshot from `source` at or after `after` (within 3 days)."""
    row = conn.execute(
        """
        SELECT sold_avg FROM price_history
        WHERE card_id = ? AND COALESCE(source, 'mock') = ? AND sold_avg IS NOT NULL
          AND run_at >= ? AND run_at <= ?
        ORDER BY run_at ASC LIMIT 1
        """,
        (card_id, source, after.isoformat(timespec="seconds"),
         (after + timedelta(days=3)).isoformat(timespec="seconds")),
    ).fetchone()
    return row["sold_avg"] if row else None


def _empty() -> dict:
    return {"right": 0, "wrong": 0, "neutral": 0}


def _rate(counts: dict) -> float | None:
    decided = counts["right"] + counts["wrong"]
    return round(counts["right"] / decided * 100, 1) if decided else None


def track_record(now: datetime | None = None) -> dict:
    """Grade every gradable call. One call per card per day counts (the day's last)."""
    now = now or datetime.now(timezone.utc)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*, c.name FROM recommendations r JOIN cards c ON c.id = r.card_id
            WHERE r.price IS NOT NULL ORDER BY r.made_at
            """).fetchall()
        latest_per_day: dict[tuple, dict] = {}
        for r in rows:
            latest_per_day[(r["card_id"], r["made_at"][:10])] = dict(r)

        result = {}
        for days in HORIZONS:
            by = {"verdict": defaultdict(_empty), "confidence": defaultdict(_empty),
                  "decided_by": defaultdict(_empty)}
            total, pending, graded = _empty(), 0, []
            for rec in latest_per_day.values():
                due = datetime.fromisoformat(rec["made_at"]) + timedelta(days=days)
                if due > now:
                    pending += 1
                    continue
                later = _later_price(conn, rec["card_id"], rec["source"], due)
                if later is None:
                    continue
                change = (later - rec["price"]) / rec["price"] * 100
                result_ = outcome(rec["verdict"], change)
                total[result_] += 1
                for key in by:
                    by[key][rec[key]][result_] += 1
                graded.append({"card": rec["name"], "made_at": rec["made_at"], "verdict": rec["verdict"],
                               "confidence": rec["confidence"], "decided_by": rec["decided_by"],
                               "price": rec["price"], "later": later, "change_pct": round(change, 1),
                               "outcome": result_})
            result[f"{days}d"] = {
                "graded": sum(total.values()), "pending": pending, **total, "hit_rate": _rate(total),
                **{f"by_{key}": {k: {**v, "hit_rate": _rate(v)} for k, v in groups.items()}
                   for key, groups in by.items()},
                "recent": sorted(graded, key=lambda g: g["made_at"], reverse=True)[:12],
            }
    return result


def prompt_summary(record_: dict | None = None) -> dict | None:
    """A compact version of the 30-day track record for the LLM's facts, or None
    until enough calls have been graded to mean anything."""
    record_ = record_ or track_record()
    month = record_.get("30d") or {}
    if month.get("graded", 0) < MIN_FOR_PROMPT:
        return None
    return {verdict: {"right": v["right"], "wrong": v["wrong"]}
            for verdict, v in month.get("by_verdict", {}).items()}
