"""Local models (fastembed, ONNX on the CPU): the embedder and the cross-encoder.

Both are small enough for an ordinary laptop and downloaded once into
data/models. Each model is loaded once per process and shared (loading takes
a few seconds; using it takes milliseconds).

Tests swap `get_embedder` / `get_reranker` for fakes, so the test suite never
downloads anything.
"""

from __future__ import annotations

import os
import threading

from . import config

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")     # harmless on Windows
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
# Small batches keep ONNX Runtime's memory low (measured: re-ranking 20 long chunks
# peaks at ~430 MB in batches of 4 vs ~1.3 GB in one batch, and isn't slower).
RERANK_BATCH = 4

# Per-model details. BGE models are trained with an instruction prefix for
# short search queries (documents are embedded as-is).
MODEL_SPECS = {
    "BAAI/bge-small-en-v1.5": {"dim": 384, "query_prefix": "Represent this sentence for searching relevant passages: "},
    "BAAI/bge-base-en-v1.5": {"dim": 768, "query_prefix": "Represent this sentence for searching relevant passages: "},
    "jinaai/jina-embeddings-v2-base-code": {"dim": 768, "query_prefix": ""},
}

_lock = threading.Lock()
_embedders: dict[str, "Embedder"] = {}
_reranker: "Reranker | None" = None


class Embedder:
    def __init__(self, model_name: str):
        from fastembed import TextEmbedding
        self.name = model_name
        self.spec = MODEL_SPECS[model_name]
        self.dim = self.spec["dim"]
        self._model = TextEmbedding(model_name, cache_dir=str(config.models_dir()))
        self._use = threading.Lock()          # one embedding call at a time per model

    def embed_documents(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """Embed many texts. They're batched shortest-first: each batch is padded to its
        longest text, so mixing a 5-line and an 80-line function wastes most of the work.
        Results come back in the original order."""
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        with self._use:
            vectors = list(self._model.embed([texts[i] for i in order], batch_size=batch_size))
        out: list[list[float]] = [[] for _ in texts]
        for pos, i in enumerate(order):
            out[i] = vectors[pos].tolist()
        return out

    def embed_query(self, text: str) -> list[float]:
        with self._use:
            return next(iter(self._model.embed([self.spec["query_prefix"] + text]))).tolist()


class Reranker:
    def __init__(self):
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        self.name = RERANK_MODEL
        self._model = TextCrossEncoder(RERANK_MODEL, cache_dir=str(config.models_dir()))
        self._use = threading.Lock()

    def score(self, query: str, documents: list[str]) -> list[float]:
        """Relevance of each document to the query (higher = more relevant)."""
        if not documents:
            return []
        with self._use:
            return [float(s) for s in self._model.rerank(query, documents, batch_size=RERANK_BATCH)]


def get_embedder(model_name: str | None = None) -> Embedder:
    name = model_name or config.get("EMBED_MODEL")
    with _lock:
        if name not in _embedders:
            _embedders[name] = Embedder(name)
        return _embedders[name]


def get_reranker() -> Reranker:
    global _reranker
    with _lock:
        if _reranker is None:
            _reranker = Reranker()
        return _reranker


def downloaded(model_name: str) -> bool:
    """Is this model already in data/models (so first use won't download)?"""
    folder = config.models_dir()
    if not folder.exists():
        return False
    short = model_name.split("/")[-1].lower()
    return any(short in p.name.lower() for p in folder.iterdir())
