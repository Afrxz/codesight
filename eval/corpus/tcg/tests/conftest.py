"""Shared test fixtures."""

import threading

import pytest

from src import config
from src.db import init_db
from src.manage import seed


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Every test gets default settings, its own .env file and its own data/.

    Without this, a key or threshold in your real .env (loaded when
    src.config is imported) would change test results, settings tests would
    write to it, and fake API responses could land in your real data/cache/.
    """
    for setting in config.SETTINGS:
        monkeypatch.delenv(setting.key, raising=False)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    # Even a test that doesn't ask for a DB must never reach your real data/app.db.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the whole app at a fresh, empty SQLite file for one test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    init_db()
    return db_path


@pytest.fixture
def seeded_db(temp_db):
    """A temp DB pre-loaded with the standard seed cards."""
    seed()
    return temp_db


@pytest.fixture
def data_dir(seeded_db, tmp_path, monkeypatch):
    """Seeded DB plus a temp data/ folder (decisions.json, caches)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def server(data_dir):
    """The binder's HTTP server on a free port, in a background thread."""
    from src.web import make_server
    srv = make_server(port=0)                 # port 0 -> any free port
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
