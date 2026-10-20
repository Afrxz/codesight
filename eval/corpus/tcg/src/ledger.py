"""The run ledger: durable checkpoints so an agent run survives a crash.

Phases 8.1–8.2. Two tables (schema in db.py):

    runs          one row per run:   in_progress -> completed | failed
    run_progress  one row per card:  pending -> done | failed, with the
                  Decision (and the market data behind it) once done

How it's used (src/run.py):
    1. start_run() writes the run row and a `pending` row for every card,
       in one transaction, before any work starts.
    2. After EACH card, checkpoint_done() / checkpoint_failed() records it.
       Nothing is held in memory until the end, so a kill loses at most the
       card that was in flight.
    3. On the next start, find_interrupted() returns the in_progress run and
       only its `pending` cards are processed.
    4. finish() marks the run completed (or failed).

Exactly-once side effects. The things a card's step writes to the database
(its price snapshot, its recommendation for self-grading, and its `done`
checkpoint) go in ONE SQLite transaction: after a crash either all of them
exist or none do, so a resumed run redoes the card cleanly or skips it.
As a second line of defence, price_history has a unique (run_id, card_id)
index, so the database itself rejects a duplicate snapshot.

Hand-rolled on purpose: a workflow engine would give the same guarantees, but
for one process looping over a few dozen cards, two tables and one
transaction per step are the whole idea, and it can be explained in full.
Limit: it assumes one agent process at a time (the binder serializes its own
runs with a lock); two processes resuming the same run could both process a
card, though the unique index still blocks a duplicate snapshot.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from .db import get_connection
from .market.types import MarketData

IN_PROGRESS, COMPLETED, FAILED = "in_progress", "completed", "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    """Sortable by start time, unique even for two runs in the same second."""
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


def start_run(cards: Sequence[Mapping], source: str, llm_mode: str, started_by: str = "cli") -> str:
    """Create the run and a pending checkpoint per card (atomically). Returns the run id."""
    run_id = new_run_id()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, cards_total, source, llm_mode, started_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, _now(), IN_PROGRESS, len(cards), source, llm_mode, started_by))
        conn.executemany(
            "INSERT INTO run_progress (run_id, card_id, position, state) VALUES (?, ?, ?, 'pending')",
            [(run_id, card["id"], i) for i, card in enumerate(cards)])
    return run_id


def find_interrupted() -> dict | None:
    """The most recent run that never finished (the process died mid-run), if any."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM runs WHERE status = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                           (IN_PROGRESS,)).fetchone()
    return dict(row) if row else None


def card_ids(run_id: str, state: str | None = None) -> list[int]:
    """The run's card ids in processing order (optionally only those in `state`)."""
    query = "SELECT card_id FROM run_progress WHERE run_id = ?"
    params: list = [run_id]
    if state:
        query += " AND state = ?"
        params.append(state)
    with get_connection() as conn:
        return [r["card_id"] for r in conn.execute(query + " ORDER BY position", params)]


def _refresh_counts(conn, run_id: str) -> None:
    conn.execute(
        """UPDATE runs SET
             cards_done   = (SELECT COUNT(*) FROM run_progress WHERE run_id = ? AND state = 'done'),
             cards_failed = (SELECT COUNT(*) FROM run_progress WHERE run_id = ? AND state = 'failed')
           WHERE run_id = ?""", (run_id, run_id, run_id))


def checkpoint_done(run_id: str, card_id: int, market: MarketData, effective: MarketData,
                    decision, source: str, attempts: int = 1) -> None:
    """Record one finished card: snapshot + recommendation + checkpoint, in ONE transaction.

    `market` is what the source reported (history records only market data);
    `effective` is what the decision used (it may carry your own price), kept
    for the binder's detail view.
    """
    from . import evaluate
    from .memory import append_snapshot

    with get_connection() as conn:
        append_snapshot(card_id, market, source=source, run_id=run_id, conn=conn)
        evaluate.record([decision], source, markets={card_id: effective}, conn=conn)
        conn.execute(
            "UPDATE run_progress SET state = 'done', decision_json = ?, market_json = ?, attempts = ?, "
            "error = NULL, updated_at = ? WHERE run_id = ? AND card_id = ?",
            (decision.model_dump_json(), effective.model_dump_json(), attempts, _now(), run_id, card_id))
        _refresh_counts(conn, run_id)


def checkpoint_failed(run_id: str, card_id: int, error: str, attempts: int = 1) -> None:
    """Record a card that couldn't be processed this run (the run carries on)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE run_progress SET state = 'failed', error = ?, attempts = ?, updated_at = ? "
            "WHERE run_id = ? AND card_id = ?", (error[:500], attempts, _now(), run_id, card_id))
        _refresh_counts(conn, run_id)


def results(run_id: str) -> tuple[list, dict[int, MarketData]]:
    """(decisions, markets) of every `done` card, in processing order.

    Read back from the checkpoints, so cards finished before a crash count
    exactly like cards finished after the resume.
    """
    from .decide import Decision

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT card_id, decision_json, market_json FROM run_progress "
            "WHERE run_id = ? AND state = 'done' ORDER BY position", (run_id,)).fetchall()
    decisions = [Decision.model_validate_json(r["decision_json"]) for r in rows]
    markets = {r["card_id"]: MarketData.model_validate_json(r["market_json"]) for r in rows}
    return decisions, markets


def finish(run_id: str, status: str = COMPLETED, error: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE run_id = ?",
                     (status, _now(), error[:500] if error else None, run_id))


def get_run(run_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def recent_runs(limit: int = 10) -> list[dict]:
    """Newest first, each with its failed cards (name + error), for the binder."""
    with get_connection() as conn:
        runs = [dict(r) for r in conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))]
        for run in runs:
            run["failed_cards"] = [dict(r) for r in conn.execute(
                "SELECT p.card_id, c.name, p.error, p.attempts FROM run_progress p "
                "JOIN cards c ON c.id = p.card_id WHERE p.run_id = ? AND p.state = 'failed' "
                "ORDER BY p.position", (run["run_id"],))]
    return runs
