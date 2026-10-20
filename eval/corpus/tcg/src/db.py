"""SQLite storage: schema definition and a connection helper.

One physical database (`data/app.db`). The core tables:

- `cards`         — the single source of truth for every tracked card. A
                    `status` flag ('watching' | 'owned') is what separates the
                    two UI views; there is deliberately only ONE data model.
- `price_history` — one row per card per run, so we can read a 30-day trend.
- `runs` / `run_progress` — the run ledger (checkpoints; see src/ledger.py).

Plus `recommendations` (self-grading) and `alerts`.

Design choices worth explaining:
- We use `sqlite3.Row` as the row factory so callers get dict-like rows
  (`row["name"]`) instead of positional tuples — clearer and refactor-safe.
- Foreign keys are enforced per-connection (SQLite leaves them OFF by default).
- `init_db()` is idempotent (`CREATE TABLE IF NOT EXISTS`), so it's safe to call
  on every startup.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import config

# Allowed values for cards.status, enforced both in the schema (CHECK) and in
# application code. Kept here as the single definition other modules import.
STATUS_WATCHING = "watching"
STATUS_OWNED = "owned"
VALID_STATUSES = (STATUS_WATCHING, STATUS_OWNED)


SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    set_code       TEXT    NOT NULL,
    grade          TEXT    NOT NULL,            -- e.g. 'PSA 10' or 'Raw' (graded vs raw are different markets)
    status         TEXT    NOT NULL CHECK (status IN ('watching', 'owned')),
    target_buy     REAL,                        -- nullable: price at/under which we'd want to buy
    target_sell    REAL,                        -- nullable: price at/over which we'd want to sell
    purchase_price REAL,                        -- nullable: only set for owned cards
    purchase_date  TEXT,                        -- nullable: ISO date string
    notes          TEXT,
    image_url      TEXT,                        -- nullable: card art for the binder UI
    variant        TEXT,                        -- nullable: finish, e.g. 'holofoil', 'reverseHolofoil'
    catalog_id     TEXT,                        -- nullable: exact pokemontcg.io card id, e.g. 'sv4pt5-232'
    manual_price   REAL,                        -- nullable: your own value, used when no source has a price
    manual_price_date TEXT,                     -- nullable: when you set it (ISO date)
    acquired       TEXT,                        -- nullable: 'bought' | 'pulled' (from a pack) | 'gift' (or trade)
    condition      TEXT                         -- nullable: raw cards' condition; NULL = Near Mint
);

CREATE TABLE IF NOT EXISTS price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id      INTEGER NOT NULL,
    run_at       TEXT    NOT NULL,              -- ISO datetime of the run
    best_listing REAL,                          -- cheapest current listing seen this run
    sold_avg     REAL,                          -- average of recent sold comps
    sold_count   INTEGER,                        -- how many comps backed sold_avg (NULL = not reported)
    source       TEXT,                           -- which MarketSource produced it ('mock', 'pokemontcg', ...)
    FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
);

-- Speeds up the common "history for one card, newest first" query.
CREATE INDEX IF NOT EXISTS idx_price_history_card_run
    ON price_history (card_id, run_at);

-- Every recommendation the agent makes, so it can grade itself later
-- against what the price actually did (src/evaluate.py).
CREATE TABLE IF NOT EXISTS recommendations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     INTEGER NOT NULL,
    made_at     TEXT    NOT NULL,               -- ISO datetime
    source      TEXT    NOT NULL,               -- which price series to grade against
    verdict     TEXT    NOT NULL,
    confidence  TEXT    NOT NULL,
    price       REAL,                           -- market price it was based on; NULL if none / your own
    decided_by  TEXT    NOT NULL,               -- 'llm' | 'rules'
    reason      TEXT,
    FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_recommendations_card ON recommendations (card_id, made_at);

-- Things the agent noticed and wants you to know (src/alerts.py).
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     INTEGER,                        -- NULL for collection-wide alerts
    kind        TEXT    NOT NULL,               -- 'price_move' | 'verdict_change' | 'target_hit' | 'dip'
    message     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    read_at     TEXT,                           -- NULL until you've seen it
    sent_at     TEXT,                           -- when it went to your webhook, if any
    FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at);

-- The run ledger (src/ledger.py): durable checkpoints, so a run that's
-- interrupted (crash, kill, power cut) resumes where it stopped.
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT    PRIMARY KEY,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    status       TEXT    NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
    cards_total  INTEGER NOT NULL,
    cards_done   INTEGER NOT NULL DEFAULT 0,
    cards_failed INTEGER NOT NULL DEFAULT 0,
    source       TEXT    NOT NULL,              -- a resumed run keeps its original settings
    llm_mode     TEXT    NOT NULL,
    started_by   TEXT,                          -- 'cli' | 'binder' | 'schedule'
    error        TEXT                           -- why the run failed, if it did
);

CREATE TABLE IF NOT EXISTS run_progress (
    run_id        TEXT    NOT NULL,
    card_id       INTEGER NOT NULL,
    position      INTEGER NOT NULL,             -- processing order within the run
    state         TEXT    NOT NULL CHECK (state IN ('pending', 'done', 'failed')),
    decision_json TEXT,                         -- the Decision, once done
    market_json   TEXT,                         -- the evidence behind it (binder detail view)
    attempts      INTEGER NOT NULL DEFAULT 0,   -- fetch attempts, incl. retries
    error         TEXT,                         -- why it failed, if it did
    updated_at    TEXT,
    PRIMARY KEY (run_id, card_id),
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE,
    FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
);
"""

# Created after the migrations, because it needs price_history.run_id, which
# an older app.db only gets via ALTER TABLE. One snapshot per card per run:
# the database itself refuses a duplicate, even if a resumed run replays a card.
# (Partial index: snapshots written outside a ledger run, e.g. demo backfills,
# have no run_id and aren't constrained.)
POST_MIGRATION_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_run_card
    ON price_history (run_id, card_id) WHERE run_id IS NOT NULL;
"""


@contextmanager
def get_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection to the SQLite database, committing and closing it.

    Use as `with get_connection() as conn: ...`. Ensures the parent `data/`
    directory exists, returns dict-like rows, and turns ON foreign-key
    enforcement (off by default in SQLite).

    Why a custom context manager: sqlite3's own `with conn:` only commits — it
    does NOT close the connection, which leaks file handles (and on Windows
    keeps the .db file locked). This one commits on success, rolls back on
    error, and always closes.

    The default path is read at call time from `config.DB_PATH`, so tests can
    point the whole app at a temp DB by patching that one value.
    """
    db_path = db_path or config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release, as (table, column, type). `CREATE TABLE
# IF NOT EXISTS` won't touch a table that already exists, so an older app.db
# gets these via ALTER TABLE instead. Append-only — never edit past entries.
MIGRATIONS = [
    ("cards", "image_url", "TEXT"),
    ("cards", "variant", "TEXT"),
    ("cards", "catalog_id", "TEXT"),
    ("cards", "manual_price", "REAL"),
    ("cards", "manual_price_date", "TEXT"),
    ("cards", "acquired", "TEXT"),
    ("cards", "condition", "TEXT"),
    # Rows written before this column existed are all from the mock source;
    # memory.get_history treats NULL as 'mock'.
    ("price_history", "source", "TEXT"),
    # Which ledger run wrote the snapshot (NULL for backfills / older rows).
    ("price_history", "run_id", "TEXT"),
]


def init_db(db_path: Path | None = None) -> None:
    """Create the tables (and index) if needed, then apply column migrations. Idempotent."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)
        for table, column, col_type in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.executescript(POST_MIGRATION_SQL)


if __name__ == "__main__":
    # Allow `python -m src.db` to initialise the database.
    init_db()
    print(f"Initialised database at {config.DB_PATH}")
