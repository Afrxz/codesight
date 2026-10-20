"""Daily auto-run: the agent runs itself while the binder is open.

A background thread in the binder server checks once a minute. If AUTO_RUN is
on, it's past RUN_TIME (local time) and today's automatic run hasn't happened,
it runs the full agent cycle. Opening the binder after the run time catches up
on a missed day. The last automatic run is remembered in data/agent_state.json,
so restarting the server doesn't run it twice.

Runs while the binder is closed: `python -m src.scheduler` runs the same
schedule headless (same settings, same state file), or point your OS
scheduler at `python -m src.run`.

Durability (Phase 8.5): the schedule's state is a file, so a restart never
loses it or runs a day twice; every firing is a new ledger run (src/ledger.py),
and a run that was interrupted is resumed at startup before the schedule's
first check (resume_interrupted_run).
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, time, timedelta

from . import config

log = logging.getLogger("tcg.scheduler")
CHECK_EVERY_S = 60


def _state_path():
    return config.DATA_DIR / "agent_state.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def run_time() -> time:
    hours, minutes = (int(x) for x in config.get("RUN_TIME").split(":"))
    return time(hours, minutes)


def is_due(now: datetime, state: dict) -> bool:
    """Pure rule (unit-tested): on, past today's run time, not yet run today."""
    if config.get("AUTO_RUN") != "on":
        return False
    return now.time() >= run_time() and state.get("last_auto_run_date") != now.date().isoformat()


def status(now: datetime | None = None) -> dict:
    """For the binder: is it on, when did it last run, when's the next one."""
    now = now or datetime.now()
    state = load_state()
    on = config.get("AUTO_RUN") == "on"
    next_run = None
    if on:
        today_run = datetime.combine(now.date(), run_time())
        if state.get("last_auto_run_date") == now.date().isoformat():
            next_run = today_run + timedelta(days=1)      # done today -> tomorrow
        elif now < today_run:
            next_run = today_run                          # later today
        else:
            next_run = now                                # due: within the next minute
        next_run = next_run.isoformat(timespec="minutes")
    return {"auto_run": on, "run_time": config.get("RUN_TIME"), "next_run": next_run,
            "last_auto_run": state.get("last_auto_run_at"), "last_error": state.get("last_error")}


class DailyScheduler(threading.Thread):
    """Checks once a minute and runs the agent when it's due."""

    def __init__(self, run_once: Callable[[], None], lock: threading.Lock,
                 clock: Callable[[], datetime] = datetime.now):
        super().__init__(name="daily-agent-run", daemon=True)
        self.run_once, self.lock, self.clock = run_once, lock, clock
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> bool:
        """One check. Returns True if it ran the agent."""
        now = self.clock()
        state = load_state()
        if not is_due(now, state):
            return False
        if not self.lock.acquire(blocking=False):   # a manual run is in progress
            return False
        try:
            log.info("Daily run starting (scheduled for %s)", config.get("RUN_TIME"))
            try:
                self.run_once()
                state.pop("last_error", None)
            except Exception as exc:
                log.exception("Daily run failed")
                state["last_error"] = str(exc)[:300]
            # Mark the day done either way, so a failing run doesn't retry every minute.
            state["last_auto_run_date"] = now.date().isoformat()
            state["last_auto_run_at"] = now.isoformat(timespec="minutes")
            _save_state(state)
            return True
        finally:
            self.lock.release()

    def run(self) -> None:
        self._stop.wait(5)   # let the server finish starting
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Scheduler check failed")
            self._stop.wait(CHECK_EVERY_S)


def resume_interrupted_run(lock: threading.Lock) -> bool:
    """Phase 8.2, at startup: if the last run never finished, resume it now.

    Holds `lock`, so a manual Re-run or the daily check waits its turn
    instead of starting a second run alongside it. True if it resumed one.
    """
    from . import ledger
    from .db import init_db
    from .run import run

    init_db()
    interrupted = ledger.find_interrupted()
    if interrupted is None:
        return False
    with lock:
        log.info("Resuming interrupted run %s (%d of %d cards done)", interrupted["run_id"],
                 interrupted["cards_done"], interrupted["cards_total"])
        try:
            run(interrupted["source"])     # run() picks the interrupted run back up
        except Exception:
            log.exception("Couldn't resume run %s", interrupted["run_id"])
            return False
    return True


def main(argv: list[str] | None = None) -> None:
    """Headless: the daily schedule without the binder open (Ctrl+C to stop)."""
    parser = argparse.ArgumentParser(description="Run the agent on its daily schedule (AUTO_RUN, RUN_TIME).")
    parser.parse_args(argv)
    from .run import run, setup_logging

    setup_logging()
    if config.get("AUTO_RUN") != "on":
        log.warning("AUTO_RUN is off, so nothing will run. Turn it on in Settings -> Agent "
                    "(or set AUTO_RUN=on in .env).")
    lock = threading.Lock()
    resume_interrupted_run(lock)
    log.info("Scheduler running: daily at %s (next: %s)", config.get("RUN_TIME"), status()["next_run"])
    daily = DailyScheduler(run_once=lambda: run(started_by="schedule"), lock=lock)
    try:
        daily.run()          # the thread's loop, in the foreground
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
