"""Agent memory: one price snapshot per card per run.

Each run appends the scalar summaries of `MarketData` to `price_history`.
Reading those rows back gives the decision layer a trend ("is the sold average
rising or falling?") and gives the dashboard its sparklines.

We store only the summaries (best_listing / sold_avg / sold_count), not the
sample listings — the trend is what we need from history, and it keeps the
table small.

Each row also records which source produced it. Mock prices and real prices
for the same card are different series, and a trend computed across both
would be meaningless, so history is always read for one source at a time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import get_connection
from .market.types import MarketData

# Rows written before the `source` column existed came from the mock source.
LEGACY_SOURCE = "mock"


def _now_iso() -> str:
    # UTC with seconds precision. ISO strings sort chronologically as text,
    # which is why ORDER BY run_at works without a datetime column type.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_snapshot(card_id: int, market: MarketData, run_at: str | None = None,
                    source: str = LEGACY_SOURCE, run_id: str | None = None, conn=None) -> int | None:
    """Write one `price_history` row for `card_id` and return its id.

    `run_at` is injectable so tests (and backfills) can control timestamps.
    `run_id` ties the row to a ledger run: a second snapshot for the same card
    in the same run is silently ignored (unique index), and None is returned.
    `conn` lets the ledger write the snapshot inside its checkpoint transaction.
    """
    sql = """
        INSERT OR IGNORE INTO price_history
            (card_id, run_at, best_listing, sold_avg, sold_count, source, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    params = (card_id, run_at or _now_iso(), market.best_listing,
              market.sold_avg, market.sold_count, source, run_id)
    if conn is not None:
        cur = conn.execute(sql, params)
    else:
        with get_connection() as own:
            cur = own.execute(sql, params)
    return int(cur.lastrowid) if cur.rowcount else None


def get_history(card_id: int, days: int = 30, source: str | None = LEGACY_SOURCE) -> list[dict]:
    """Return this card's snapshots from one source over the last `days` days, oldest first.

    Oldest-first is the natural order for reading a trend or drawing a line.
    `source=None` returns every source (only useful for debugging).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    query = """
        SELECT run_at, best_listing, sold_avg, sold_count
        FROM price_history
        WHERE card_id = ? AND run_at >= ?
    """
    params: list = [card_id, cutoff]
    if source is not None:
        query += " AND COALESCE(source, ?) = ?"
        params += [LEGACY_SOURCE, source]
    query += " ORDER BY run_at ASC, id ASC"
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]
