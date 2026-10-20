"""Retrieval: find the code chunks that answer a question.

    question ─┬─ dense search (embeddings: meaning)      ─┐
              └─ sparse search (BM25: exact identifiers) ─┴─ RRF fusion ─ cross-encoder rerank ─ top k

- Dense search finds code by meaning ("where are failed requests retried?"
  finds `fetch_with_retry` even though no word matches).
- BM25 finds exact names and strings the embedding model blurs.
- Reciprocal Rank Fusion merges the two ranked lists using ranks only (their
  scores aren't comparable): score(chunk) = sum over lists of 1 / (k + rank).
  A chunk ranked high by either search rises; one ranked by both rises most.
- The cross-encoder then reads the question and each candidate together and
  re-orders the best candidates. It's slower but much more accurate than either
  search, so it's only run on the top few.

Every stage's rank, score and timing is returned, so the UI can show how an
answer's sources were found, and the eval can switch stages on and off.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from qdrant_client import models as qm

from . import bm25, config, store
from .models import get_embedder, get_reranker


@dataclass
class Hit:
    id: str
    path: str
    start_line: int
    end_line: int
    kind: str
    symbol: str
    language: str | None
    text: str
    context: str = ""
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    rank: int = 0                       # final position, 1-based

    def embedding_text(self) -> str:
        head = self.path + (f" · {self.symbol}" if self.symbol else "")
        return "\n".join(x for x in (head, self.context, self.text) if x)

    def view(self) -> dict:
        return asdict(self)


@dataclass
class Retrieval:
    hits: list[Hit]
    mode: str
    reranked: bool
    timings_ms: dict = field(default_factory=dict)
    candidates: int = 0                 # how many distinct chunks the searches found


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion over ranked lists of ids (best first)."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def _hit(point) -> Hit:
    p = point.payload
    return Hit(id=str(point.id), path=p["path"], start_line=p["start_line"], end_line=p["end_line"],
               kind=p["kind"], symbol=p["symbol"], language=p.get("language"), text=p["text"],
               context=p.get("context", ""))


def dense_search(collection: str, model_name: str, question: str, limit: int) -> list:
    vector = get_embedder(model_name).embed_query(question)
    return store.client().query_points(collection, query=vector, using="dense", limit=limit,
                                       with_payload=True).points


def sparse_search(collection: str, question: str, limit: int) -> list:
    idx, val = bm25.query_vector(question)
    if not idx:
        return []
    return store.client().query_points(collection, query=qm.SparseVector(indices=idx, values=val),
                                       using="bm25", limit=limit, with_payload=True).points


def retrieve(repo: dict, question: str, mode: str | None = None, rerank: bool | None = None,
             top_k: int | None = None, candidates: int | None = None, rerank_candidates: int | None = None,
             rrf_k: int | None = None) -> Retrieval:
    """Run the pipeline for one question. Unset options come from Settings."""
    mode = mode or config.get("RETRIEVAL_MODE")
    rerank = (config.get("RERANK") == "on") if rerank is None else rerank
    top_k = top_k or config.get("TOP_K")
    candidates = candidates or config.get("CANDIDATES")
    rerank_candidates = rerank_candidates or config.get("RERANK_CANDIDATES")
    rrf_k = rrf_k or config.get("RRF_K")
    collection, model_name = repo["collection"], repo["embed_model"]
    timings: dict[str, float] = {}
    by_id: dict[str, Hit] = {}

    def timed(name, fn, *args):
        t = time.perf_counter()
        result = fn(*args)
        timings[name] = round((time.perf_counter() - t) * 1000, 1)
        return result

    dense_ids: list[str] = []
    sparse_ids: list[str] = []
    if mode in ("hybrid", "dense"):
        for rank, point in enumerate(timed("dense_ms", dense_search, collection, model_name, question,
                                           candidates), 1):
            hit = by_id.setdefault(str(point.id), _hit(point))
            hit.dense_rank, hit.dense_score = rank, round(point.score, 4)
            dense_ids.append(hit.id)
    if mode in ("hybrid", "sparse"):
        for rank, point in enumerate(timed("sparse_ms", sparse_search, collection, question, candidates), 1):
            hit = by_id.setdefault(str(point.id), _hit(point))
            hit.sparse_rank, hit.sparse_score = rank, round(point.score, 4)
            sparse_ids.append(hit.id)

    # Fuse (hybrid) or keep the single list's order.
    t = time.perf_counter()
    if mode == "hybrid":
        fused = rrf([dense_ids, sparse_ids], rrf_k)
        for hit_id, score in fused.items():
            by_id[hit_id].rrf_score = round(score, 6)
        ordered = sorted(fused, key=lambda i: -fused[i])
    else:
        ordered = dense_ids if mode == "dense" else sparse_ids
    timings["fuse_ms"] = round((time.perf_counter() - t) * 1000, 2)

    hits = [by_id[i] for i in ordered]
    if rerank and hits:
        pool = hits[:rerank_candidates]
        scores = timed("rerank_ms", get_reranker().score, question, [h.embedding_text() for h in pool])
        for h, s in zip(pool, scores):
            h.rerank_score = round(s, 4)
        hits = sorted(pool, key=lambda h: -h.rerank_score) + hits[rerank_candidates:]
    hits = hits[:top_k]
    for i, h in enumerate(hits, 1):
        h.rank = i
    timings["total_ms"] = round(sum(v for k, v in timings.items() if k != "total_ms"), 1)
    return Retrieval(hits=hits, mode=mode, reranked=bool(rerank and hits), timings_ms=timings,
                     candidates=len(by_id))
