"""Phase 8.4 — kill the agent mid-run for real, restart it, check the end state.

Not a simulation: the run happens in a separate Python process, which is
hard-killed (TerminateProcess / SIGKILL: no cleanup code runs) while it's
fetching card 4 of 8. A second process then starts the agent again, and the
test checks the database: one ledger run, completed, all 8 cards done, and
exactly one price snapshot and one recommendation per card.

    pytest tests/test_crash_resume.py -v -s     # -s shows the steps
"""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOTAL_CARDS, KILL_AFTER = 8, 3

# The agent process. argv: data dir, "slow" (first process) or "normal" (restart).
# In the slow process each fetch takes a moment and the 4th never returns, so
# the kill always lands mid-card. Everything else is the real run loop.
CHILD = r"""
import sys, time
from pathlib import Path
from src import config
config.DATA_DIR = Path(sys.argv[1])
config.DB_PATH = config.DATA_DIR / "app.db"
from src.market.mock import MockSource
if sys.argv[2] == "slow":
    original, calls = MockSource.fetch_market_data, {"n": 0}
    def slow(self, *args, **kwargs):
        calls["n"] += 1
        time.sleep(60 if calls["n"] == 4 else 0.2)
        return original(self, *args, **kwargs)
    MockSource.fetch_market_data = slow
from src.run import run
run("mock", "off")
"""


def _env():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # No keys or webhook in the child: mock source + rules only, nothing leaves the machine.
    for key in ("LLM_API_KEY", "JUSTTCG_API_KEY", "TCGAPI_KEY", "POKEMONTCG_API_KEY", "ALERT_WEBHOOK_URL"):
        env[key] = ""
    return env


def _agent(data_dir: Path, mode: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", CHILD, str(data_dir), mode], cwd=PROJECT_ROOT,
                            env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _query(db: Path, sql: str):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_kill_mid_run_then_resume(tmp_path, monkeypatch):
    from src import config
    from src.manage import add_card, seed

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DB_PATH", data_dir / "app.db")
    seed()                                                         # 7 example cards...
    add_card("Pikachu 25/25", "CEL", "Raw", "watching")             # ...+1 = 8
    db = data_dir / "app.db"

    # 1. Start the agent; kill it once 3 cards are checkpointed.
    proc = _agent(data_dir, "slow")
    deadline = time.time() + 60
    done = 0
    while time.time() < deadline:
        rows = _query(db, "SELECT cards_done FROM runs") if db.exists() else []
        done = rows[0][0] if rows and rows[0][0] is not None else 0
        if done >= KILL_AFTER:
            break
        time.sleep(0.05)
    proc.kill()                                                    # hard kill, mid card 4
    proc.wait(timeout=10)
    print(f"\nkilled the agent after {done} of {TOTAL_CARDS} cards")
    assert done == KILL_AFTER
    [(status, cards_done)] = _query(db, "SELECT status, cards_done FROM runs")
    assert status == "in_progress" and cards_done == KILL_AFTER
    assert _query(db, "SELECT COUNT(*) FROM price_history")[0][0] == KILL_AFTER

    # 2. Restart: a fresh process finds the interrupted run and finishes it.
    restart = _agent(data_dir, "normal")
    assert restart.wait(timeout=60) == 0

    # 3. One run, completed, every card done once.
    runs = _query(db, "SELECT status, cards_total, cards_done, cards_failed FROM runs")
    print(f"after restart: {runs}")
    assert runs == [("completed", TOTAL_CARDS, TOTAL_CARDS, 0)]
    assert _query(db, "SELECT COUNT(*) FROM run_progress WHERE state = 'done'")[0][0] == TOTAL_CARDS
    per_card = _query(db, "SELECT card_id, COUNT(*) FROM price_history GROUP BY card_id")
    assert len(per_card) == TOTAL_CARDS and all(n == 1 for _, n in per_card)    # no duplicates
    recs = _query(db, "SELECT card_id, COUNT(*) FROM recommendations GROUP BY card_id")
    assert len(recs) == TOTAL_CARDS and all(n == 1 for _, n in recs)
