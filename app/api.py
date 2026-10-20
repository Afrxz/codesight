"""The web API (FastAPI) and the UI's static files.

    GET    /                          the UI
    GET    /api/health                models downloaded? provider key set? storage mode
    GET    /api/repos                 indexed repos
    POST   /api/repos                 {source, name?} add + start indexing -> job
    POST   /api/repos/{id}/reindex    update the index (only changed files) -> job
    DELETE /api/repos/{id}            remove a repo and its index
    GET    /api/jobs/{job_id}         indexing progress
    POST   /api/ask                   {repo_id, question, history} -> server-sent events
    GET    /api/repos/{id}/file       ?path=&start=&end= source lines for the code viewer
    GET    /api/settings              all settings (secrets masked)
    POST   /api/settings              {values, remove} validate + save
    POST   /api/settings/test         {provider, key?} one tiny live call
    GET    /api/eval                  the latest eval results

Safety: the server only listens on 127.0.0.1. Every state-changing request
must carry an `X-RAGG: 1` header, which a web page on another site can't send
without a CORS preflight this server never approves (a CSRF guard). The file
endpoint only serves files that are part of an indexed repo.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, indexer, llm, models, store
from .chunking import CHUNKER_VERSION
from .generation import ask

log = logging.getLogger("ragg.api")
WEB_DIR = config.PROJECT_ROOT / "web"
EVAL_DIR = config.PROJECT_ROOT / "eval" / "results"

app = FastAPI(title="RAGG — codebase Q&A", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("x-ragg") != "1":
        return JSONResponse({"detail": "Missing X-RAGG header"}, status_code=403)
    return await call_next(request)


# --- models ---------------------------------------------------------------------

class AddRepo(BaseModel):
    source: str = Field(min_length=1, max_length=1000)
    name: str | None = Field(default=None, max_length=80)


class Turn(BaseModel):
    question: str = Field(max_length=4000)
    answer: str = Field(max_length=20000)


class Ask(BaseModel):
    repo_id: str
    question: str = Field(min_length=1, max_length=4000)
    history: list[Turn] = Field(default_factory=list, max_length=10)


class SaveSettings(BaseModel):
    values: dict = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)


class TestKey(BaseModel):
    provider: str
    key: str | None = Field(default=None, max_length=500)


def _repo_or_404(repo_id: str) -> dict:
    repo = store.get_repo(repo_id)
    if repo is None:
        raise HTTPException(404, "No such repo")
    return repo


def _repo_view(repo: dict) -> dict:
    view = store.public_repo(repo)
    job = indexer.active_job(repo["id"])
    view["job"] = job.view() if job else None
    view["stale"] = bool(repo.get("collection")) and (repo.get("chunker") != config.get("CHUNKER")
                                                      or repo.get("embed_model") != config.get("EMBED_MODEL")
                                                      or repo.get("chunker_version") != CHUNKER_VERSION)
    return view


# --- pages ----------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index_page():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health():
    provider = config.get("LLM_PROVIDER")
    return {"provider": provider, "provider_label": config.PROVIDER_LABELS[provider], "model": config.llm_model(),
            "key_set": bool(config.get(config.PROVIDER_KEYS[provider])),
            "embed_model": config.get("EMBED_MODEL"),
            "embed_downloaded": models.downloaded(config.get("EMBED_MODEL")),
            "rerank_downloaded": models.downloaded(models.RERANK_MODEL),
            "storage": "server" if config.get("QDRANT_URL") else "embedded"}


# --- repos ----------------------------------------------------------------------

@app.get("/api/repos")
def list_repos():
    repos = sorted(store.load_registry().values(), key=lambda r: r["name"].lower())
    return [_repo_view(r) for r in repos]


@app.post("/api/repos")
def add_repo(body: AddRepo):
    try:
        repo = indexer.new_repo(body.source, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    job = indexer.start(repo["id"])
    return {"repo": _repo_view(store.get_repo(repo["id"])), "job": job.view()}


@app.post("/api/repos/{repo_id}/reindex")
def reindex_repo(repo_id: str):
    _repo_or_404(repo_id)
    return {"job": indexer.start(repo_id).view()}


@app.delete("/api/repos/{repo_id}")
def delete_repo(repo_id: str):
    if indexer.active_job(repo_id):
        raise HTTPException(409, "It's being indexed; try again when that finishes.")
    if not indexer.remove(repo_id):
        raise HTTPException(404, "No such repo")
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = indexer.get_job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job.view()


@app.get("/api/repos/{repo_id}/file")
def read_file(repo_id: str, path: str, start: int = 1, end: int | None = None):
    """Lines of one indexed file. Only files in the repo's index are served."""
    repo = _repo_or_404(repo_id)
    if path not in repo.get("files", {}) or not repo.get("root"):
        raise HTTPException(404, "Not an indexed file of this repo")
    root = Path(repo["root"]).resolve()
    full = (root / path).resolve()
    if root not in full.parents or not full.is_file():
        raise HTTPException(404, "Not an indexed file of this repo")
    lines = full.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    start = max(1, start)
    end = min(len(lines), end or len(lines))
    return {"path": path, "language": repo["files"][path].get("language"), "start": start, "end": end,
            "total_lines": len(lines), "lines": lines[start - 1:end]}


# --- asking -----------------------------------------------------------------------

@app.post("/api/ask")
def ask_question(body: Ask):
    repo = _repo_or_404(body.repo_id)
    if repo.get("status") != "ready" and not repo.get("collection"):
        raise HTTPException(409, "This repo hasn't finished indexing yet.")
    history = [t.model_dump() for t in body.history]

    def events():
        try:
            for event in ask(repo, body.question, history):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:          # anything unexpected still reaches the UI as a message
            log.exception("Ask failed")
            yield f"data: {json.dumps({'type': 'error', 'message': f'{type(exc).__name__}: {exc}'})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- settings -------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    return {"settings": config.public_view(), "providers": [
        {"id": p, "label": config.PROVIDER_LABELS[p], "default_model": config.DEFAULT_MODELS[p],
         "key": config.PROVIDER_KEYS[p]} for p in config.PROVIDERS]}


@app.post("/api/settings")
def save_settings(body: SaveSettings):
    try:
        config.save(body.values, body.remove)
    except config.ConfigError as exc:
        raise HTTPException(400, str(exc)) from None
    return get_settings()


@app.post("/api/settings/test")
def test_key(body: TestKey):
    if body.provider not in config.PROVIDERS:
        raise HTTPException(400, "Unknown provider")
    ok, message = llm.check_key(body.provider, body.key or None)
    return {"ok": ok, "message": message}


# --- eval -------------------------------------------------------------------------

@app.get("/api/eval")
def latest_eval():
    runs = sorted(p for p in EVAL_DIR.glob("*") if (p / "summary.json").exists()) if EVAL_DIR.exists() else []
    if not runs:
        return {"run": None}
    return {"run": json.loads((runs[-1] / "summary.json").read_text(encoding="utf-8")),
            "runs": [p.name for p in runs[-10:]]}


# --- startup ----------------------------------------------------------------------

def warm_up() -> None:
    """Load the models in the background, so the first question isn't slow."""
    def work():
        try:
            if store.load_registry():
                models.get_embedder()
                if config.get("RERANK") == "on":
                    models.get_reranker()
                log.info("Models loaded")
        except Exception:
            log.exception("Model warm-up failed (they'll load on first use)")
    threading.Thread(target=work, name="warm-up", daemon=True).start()
