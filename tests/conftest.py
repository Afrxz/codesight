"""Shared fixtures. Tests are offline: no model downloads, no API calls.

- Settings and data/ point at a temp folder (your real .env and index are never touched).
- The embedder is a deterministic bag-of-words hash (similar words -> similar vectors),
  the reranker scores word overlap, and Qdrant runs in memory.
"""

import hashlib
import math
import re
import textwrap
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from app import config, models, store

FAKE_DIM = 64


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower()) if len(w) > 2]


class FakeEmbedder:
    name = "fake"
    dim = FAKE_DIM

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * FAKE_DIM
        for w in _words(text):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % FAKE_DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts, batch_size=32):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class FakeReranker:
    name = "fake-reranker"

    def score(self, query, documents):
        q = set(_words(query))
        return [len(q & set(_words(d))) / (len(q) or 1) for d in documents]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for s in config.SETTINGS:
        monkeypatch.delenv(s.key, raising=False)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    fake_embedder, fake_reranker = FakeEmbedder(), FakeReranker()
    for module in ("app.models", "app.indexer", "app.retrieval"):
        monkeypatch.setattr(f"{module}.get_embedder", lambda name=None: fake_embedder, raising=False)
    monkeypatch.setattr("app.retrieval.get_reranker", lambda: fake_reranker)
    monkeypatch.setattr(models, "get_reranker", lambda: fake_reranker)
    store.use_client(QdrantClient(":memory:"))
    yield
    store.use_client(None)


@pytest.fixture
def sample_repo(tmp_path) -> Path:
    """A tiny multi-language repo with known functions."""
    root = tmp_path / "sample"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "net.py").write_text(textwrap.dedent('''\
        """Networking helpers."""
        import time

        MAX_ATTEMPTS = 3


        def fetch_with_retry(url, attempts=MAX_ATTEMPTS):
            """Download a URL, retrying with exponential backoff on timeouts."""
            for attempt in range(attempts):
                try:
                    return download(url)
                except TimeoutError:
                    time.sleep(2 ** attempt)
            raise RuntimeError("gave up")


        class Cache:
            """An in-memory cache with expiry."""

            def __init__(self, ttl):
                self.ttl = ttl
                self.items = {}

            def get(self, key):
                return self.items.get(key)

            @staticmethod
            def clear_everything():
                return None
        '''), encoding="utf-8")
    (root / "web").mkdir()
    (root / "web" / "app.js").write_text(textwrap.dedent('''\
        import { api } from "./api.js";

        export function renderInvoice(invoice) {
          return `Total: ${invoice.total}`;
        }

        const parseDate = (text) => new Date(text);

        class ShoppingCart {
          addItem(item) { this.items.push(item); }
          totalPrice() { return this.items.reduce((s, i) => s + i.price, 0); }
        }
        '''), encoding="utf-8")
    (root / "README.md").write_text("# Sample\n\nA sample project about invoices and carts.\n", encoding="utf-8")
    (root / ".gitignore").write_text("secret/\n*.log\n", encoding="utf-8")
    (root / "secret").mkdir()
    (root / "secret" / "keys.py").write_text("API_KEY = 'nope'\n", encoding="utf-8")
    (root / "debug.log").write_text("noise\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "lib.js").write_text("function vendored() {}\n", encoding="utf-8")
    (root / "image.png").write_bytes(b"\x89PNG\x00\x00binary")
    return root
