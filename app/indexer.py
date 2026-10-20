"""Indexing: repo -> files -> chunks -> (dense + BM25 vectors) -> Qdrant.

    index_repo(source)     add a repo (local folder or https git URL) and index it
    reindex(repo_id)       bring an index up to date

Incremental: each file's content hash is remembered, so a re-index only
re-chunks and re-embeds files that changed (and drops deleted ones). If the
chunker or embedding model setting changed since the last build, the repo is
rebuilt from scratch into a fresh collection instead, because mixing chunk
types or vector spaces in one index would make results meaningless.

Jobs run in a background thread and report progress (phase, done/total), so
the UI stays responsive while a big repo indexes on a slow laptop.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from qdrant_client import models as qm

from . import bm25, config, store
from .chunking import CHUNKER_VERSION, Chunk, chunk_file
from .loader import clone, is_git_url, load_files, repo_slug
from .models import get_embedder

log = logging.getLogger("ragg.indexer")
UPSERT_BATCH = 128


@dataclass
class Job:
    id: str
    repo_id: str
    phase: str = "queued"            # queued | loading | chunking | embedding | saving | done | error
    done: int = 0
    total: int = 0
    message: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: dict | None = None

    def view(self) -> dict:
        return {"id": self.id, "repo_id": self.repo_id, "phase": self.phase, "done": self.done,
                "total": self.total, "message": self.message,
                "elapsed_s": round((self.finished or time.time()) - self.started, 1), "result": self.result}


_jobs: dict[str, Job] = {}
_repo_locks: dict[str, threading.Lock] = {}
_jobs_lock = threading.Lock()


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def active_job(repo_id: str) -> Job | None:
    return next((j for j in _jobs.values() if j.repo_id == repo_id and j.phase not in ("done", "error")), None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _collection_name(repo_id: str, chunker: str, model: str) -> str:
    digest = hashlib.sha1(f"{chunker}|{model}|v{CHUNKER_VERSION}".encode()).hexdigest()[:8]
    return f"ragg_{repo_id}_{digest}"


def new_repo(source: str, name: str | None = None) -> dict:
    """Register a repo (no indexing yet). `source` is a folder path or an https git URL."""
    source = source.strip().strip('"')
    if not source:
        raise ValueError("Give a folder path or an https:// git URL.")
    if not is_git_url(source):
        root = Path(source).expanduser()
        if not root.is_dir():
            raise ValueError(f"Folder not found: {source}")
    base = repo_slug(source)
    registry = store.load_registry()
    for repo in registry.values():
        if repo["source"] == source:
            raise ValueError(f"Already added as “{repo['name']}”. Use Re-index to update it.")
    repo_id, n = base, 2
    while repo_id in registry:
        repo_id, n = f"{base}-{n}", n + 1
    repo = {"id": repo_id, "name": name or base, "source": source, "is_git": is_git_url(source),
            "root": None, "status": "new", "chunker": None, "embed_model": None, "collection": None,
            "files": {}, "stats": {}, "avgdl": None, "indexed_at": None, "error": None}
    store.put_repo(repo)
    return repo


def start(repo_id: str) -> Job:
    """Start (or return the running) index job for a repo, in a background thread."""
    with _jobs_lock:
        running = active_job(repo_id)
        if running:
            return running
        job = Job(id=uuid.uuid4().hex[:12], repo_id=repo_id)
        _jobs[job.id] = job
    threading.Thread(target=_run_job, args=(job,), name=f"index-{repo_id}", daemon=True).start()
    return job


def _run_job(job: Job) -> None:
    lock = _repo_locks.setdefault(job.repo_id, threading.Lock())
    with lock:
        try:
            job.result = reindex(job.repo_id, progress=job)
            job.phase = "done"
            job.message = (f"Indexed {job.result['files']} files → {job.result['chunks']} chunks "
                           f"({job.result['changed_files']} changed) in {job.result['seconds']} s")
        except Exception as exc:
            log.error("Indexing %s failed: %s", job.repo_id, traceback.format_exc())
            job.phase, job.message = "error", f"{type(exc).__name__}: {exc}"
            repo = store.get_repo(job.repo_id)
            if repo:
                repo["status"], repo["error"] = "error", job.message
                store.put_repo(repo)
        finally:
            job.finished = time.time()


def _set(progress: Job | None, phase: str, done: int = 0, total: int = 0, message: str = "") -> None:
    if progress is not None:
        progress.phase, progress.done, progress.total, progress.message = phase, done, total, message


def reindex(repo_id: str, progress: Job | None = None, force: bool = False, chunker: str | None = None) -> dict:
    """Index or update one repo, synchronously. Returns stats.
    `chunker` overrides the setting (the eval uses it to measure variants)."""
    started = time.time()
    repo = store.get_repo(repo_id)
    if repo is None:
        raise KeyError(f"No repo {repo_id!r}")
    repo["status"], repo["error"] = "indexing", None
    store.put_repo(repo)

    # 1. Files
    _set(progress, "loading", message="Reading files…")
    if repo["is_git"]:
        _set(progress, "loading", message="Cloning / updating the git repo…")
        root = clone(repo["source"], config.repos_dir() / repo_id)
    else:
        root = Path(repo["source"]).expanduser()
    files = load_files(root, max_kb=config.get("MAX_FILE_KB"), extra_ignore=config.get("EXTRA_IGNORE"))
    if not files:
        raise ValueError("No indexable source files found in that folder.")

    chunker, model_name = chunker or config.get("CHUNKER"), config.get("EMBED_MODEL")
    # A different chunker, model or chunking-rules version means different chunks or vectors:
    # rebuild from scratch rather than mix them in one index.
    rebuild = (force or repo["chunker"] != chunker or repo["embed_model"] != model_name
               or repo.get("chunker_version") != CHUNKER_VERSION or not repo["collection"])
    old_files: dict = {} if rebuild else repo["files"]
    current = {f.path: f for f in files}
    changed = [f for f in files if old_files.get(f.path, {}).get("sha") != f.sha]
    removed = [p for p in old_files if p not in current]

    # 2. Chunks (+ token counts for BM25's average document length)
    _set(progress, "chunking", 0, len(changed), "Splitting code into chunks…")
    new_chunks: list[Chunk] = []
    file_stats = {p: s for p, s in old_files.items() if p in current and p not in {f.path for f in changed}}
    tokens_by_chunk: dict[str, list[str]] = {}
    for i, f in enumerate(changed, 1):
        chunks = chunk_file(f, chunker, config.get("WINDOW_LINES"), config.get("WINDOW_OVERLAP"))
        n_tokens = 0
        for c in chunks:
            toks = bm25.tokenize(c.embedding_text())
            tokens_by_chunk[c.id] = toks
            n_tokens += len(toks)
        file_stats[f.path] = {"sha": f.sha, "chunks": len(chunks), "tokens": n_tokens, "language": f.language}
        new_chunks.extend(chunks)
        if i % 20 == 0:
            _set(progress, "chunking", i, len(changed), "Splitting code into chunks…")
    total_chunks = sum(s["chunks"] for s in file_stats.values())
    avgdl = sum(s["tokens"] for s in file_stats.values()) / max(total_chunks, 1)

    # 3. Embeddings
    embedder = get_embedder(model_name)
    collection = _collection_name(repo_id, chunker, model_name)
    if rebuild:
        if repo["collection"] and repo["collection"] != collection:
            store.drop_collection(repo["collection"])
        store.create_collection(collection, embedder.dim)
    else:
        store.delete_paths(collection, [f.path for f in changed if f.path in old_files] + removed)

    batch = config.get("EMBED_BATCH")
    points: list[qm.PointStruct] = []
    _set(progress, "embedding", 0, len(new_chunks), "Embedding chunks…")
    # Embedded in groups of a few batches (so progress updates), shortest chunks first
    # within the whole job: similar lengths in a batch means far less padding.
    new_chunks.sort(key=lambda c: len(c.embedding_text()))
    group = batch * 4
    for start_i in range(0, len(new_chunks), group):
        part = new_chunks[start_i:start_i + group]
        vectors = embedder.embed_documents([c.embedding_text() for c in part], batch_size=batch)
        for c, vec in zip(part, vectors):
            idx, val = bm25.doc_vector(tokens_by_chunk[c.id], avgdl)
            points.append(qm.PointStruct(
                id=c.id,
                vector={"dense": vec, "bm25": qm.SparseVector(indices=idx, values=val)},
                payload={"path": c.path, "language": c.language, "start_line": c.start_line,
                         "end_line": c.end_line, "kind": c.kind, "symbol": c.symbol, "text": c.text,
                         "context": c.context, "meta": c.meta}))
        _set(progress, "embedding", min(start_i + group, len(new_chunks)), len(new_chunks), "Embedding chunks…")

    # 4. Save
    _set(progress, "saving", 0, len(points), "Saving to the index…")
    c = store.client()
    for start_i in range(0, len(points), UPSERT_BATCH):
        c.upsert(collection, points=points[start_i:start_i + UPSERT_BATCH], wait=True)
        _set(progress, "saving", min(start_i + UPSERT_BATCH, len(points)), len(points), "Saving to the index…")

    languages: dict[str, int] = {}
    for s in file_stats.values():
        languages[s["language"] or "text"] = languages.get(s["language"] or "text", 0) + 1
    repo.update({
        "root": str(root.resolve()), "status": "ready", "chunker": chunker, "embed_model": model_name,
        "chunker_version": CHUNKER_VERSION,
        "collection": collection, "files": file_stats, "avgdl": round(avgdl, 2), "indexed_at": _now(),
        "stats": {"files": len(file_stats), "chunks": total_chunks, "languages": languages},
        "error": None,
    })
    store.put_repo(repo)
    result = {"files": len(file_stats), "chunks": total_chunks, "changed_files": len(changed),
              "removed_files": len(removed), "new_chunks": len(new_chunks), "rebuilt": rebuild,
              "seconds": round(time.time() - started, 1)}
    log.info("Indexed %s: %s", repo_id, result)
    return result


def remove(repo_id: str) -> bool:
    repo = store.remove_repo(repo_id)
    if repo is None:
        return False
    if repo.get("collection"):
        store.drop_collection(repo["collection"])
    if repo.get("is_git"):
        # Our own shallow clone (never a folder the user pointed us at).
        shutil.rmtree(config.repos_dir() / repo_id, ignore_errors=True)
    return True
