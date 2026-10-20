"""Storage: the Qdrant vector database, and the registry of indexed repos.

Qdrant holds one collection per indexed repo. Each point is one chunk with two
named vectors:
    "dense"  the embedding (cosine similarity)
    "bm25"   the BM25 sparse vector (Qdrant applies the idf term: Modifier.IDF)
and the chunk itself as payload (path, lines, symbol, text, ...).

By default Qdrant runs embedded (a folder under data/, no server to install).
Embedded mode allows one process at a time per folder; set QDRANT_URL to use
a Qdrant server instead.

The registry (data/repos.json) remembers, per repo: where it came from, which
chunker and embedding model built its index, per-file hashes (for incremental
re-indexing) and BM25's average chunk length.
"""

from __future__ import annotations

import atexit
import json
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path

from qdrant_client import QdrantClient, models

from . import config

_client: QdrantClient | None = None
_client_key: tuple | None = None
_injected: QdrantClient | None = None          # tests: an in-memory client
_client_lock = threading.Lock()
_registry_lock = threading.RLock()


# --- Qdrant -----------------------------------------------------------------------

def client() -> QdrantClient:
    """The shared Qdrant client (rebuilt if the storage settings changed)."""
    global _client, _client_key
    if _injected is not None:
        return _injected
    url, key, folder = config.get("QDRANT_URL"), config.get("QDRANT_API_KEY"), str(config.qdrant_dir())
    wanted = (url, key) if url else (folder,)
    with _client_lock:
        if _client is None or _client_key != wanted:
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
            if url:
                _client = QdrantClient(url=url, api_key=key or None, timeout=30)
            else:
                Path(folder).mkdir(parents=True, exist_ok=True)
                _client = QdrantClient(path=folder)
            _client_key = wanted
        return _client


@atexit.register
def _close() -> None:
    """Close cleanly at exit (embedded Qdrant otherwise complains while Python shuts down)."""
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass


def use_client(c: QdrantClient | None) -> None:
    """Tests: route everything to the given client (None = back to the settings)."""
    global _injected
    _injected = c


def create_collection(name: str, dim: int) -> None:
    c = client()
    if c.collection_exists(name):
        c.delete_collection(name)
    c.create_collection(
        name,
        vectors_config={"dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    with warnings.catch_warnings():            # embedded mode warns that indexes are a no-op
        warnings.simplefilter("ignore")
        try:
            c.create_payload_index(name, "path", models.PayloadSchemaType.KEYWORD)
        except Exception:
            pass                               # embedded mode: filters work without it


def delete_paths(name: str, paths: list[str]) -> None:
    if not paths:
        return
    client().delete(name, points_selector=models.FilterSelector(filter=models.Filter(
        must=[models.FieldCondition(key="path", match=models.MatchAny(any=paths))])))


def drop_collection(name: str) -> None:
    c = client()
    if c.collection_exists(name):
        c.delete_collection(name)


# --- registry ---------------------------------------------------------------------

def _registry_path() -> Path:
    return config.DATA_DIR / "repos.json"


def load_registry() -> dict:
    with _registry_lock:
        path = _registry_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}


def save_registry(registry: dict) -> None:
    with _registry_lock:
        path = _registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(registry, indent=2), encoding="utf-8")
        tmp.replace(path)                       # atomic: never a half-written registry


def get_repo(repo_id: str) -> dict | None:
    return load_registry().get(repo_id)


def put_repo(repo: dict) -> None:
    with _registry_lock:
        reg = load_registry()
        repo["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        reg[repo["id"]] = repo
        save_registry(reg)


def remove_repo(repo_id: str) -> dict | None:
    with _registry_lock:
        reg = load_registry()
        repo = reg.pop(repo_id, None)
        save_registry(reg)
        return repo


def public_repo(repo: dict) -> dict:
    """What the UI needs (not the per-file hash table)."""
    return {k: v for k, v in repo.items() if k != "files"}
