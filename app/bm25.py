"""BM25 as sparse vectors, with a tokenizer that understands code.

BM25 scores a document d for query q as

    sum over query terms t:  idf(t) * tf(t,d) * (k1 + 1) / (tf(t,d) + k1 * (1 - b + b * |d| / avgdl))

We split that in two so a vector database can do it:
- the document side (everything after idf) is computed here at index time and
  stored as a sparse vector: {token id: weight};
- the idf side is computed by Qdrant at query time from its own document
  counts (the collection's sparse vector has `modifier=IDF`), so it stays right
  as files are added and removed.
The query is a sparse vector with weight 1 per query token.

The tokenizer is what makes keyword search work on code:
- `fetch_with_retry` -> fetch_with_retry, fetch, with, retry
- `getMarketData`    -> getmarketdata, get, market, data
- "retries" matches "retry" (plural folding), case doesn't matter, and question
  words ("where", "how", ...) are dropped.
"""

from __future__ import annotations

import re
import zlib
from collections import Counter

K1 = 1.2
B = 0.75

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d{2,}")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "is", "it", "and", "or", "for", "on", "at", "by", "be", "as", "this",
    "that", "with", "from", "are", "was", "how", "what", "where", "which", "why", "when", "who", "does", "do",
    "did", "can", "i", "you", "we", "me", "my", "there", "their", "its", "into", "about", "work", "works",
}


def _fold(word: str) -> str:
    """Light plural folding: retries -> retry, classes -> class, cards -> card."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Code-aware tokens (see the module docstring). Order kept, duplicates kept (for tf)."""
    out: list[str] = []
    for raw in _TOKEN.findall(text):
        if raw.isdigit():
            out.append(raw)
            continue
        parts = [p for piece in raw.split("_") for p in _CAMEL.findall(piece)]
        whole = raw.lower().strip("_")
        if whole and whole not in STOPWORDS:
            out.append(_fold(whole))
        if len(parts) > 1:
            for p in parts:
                p = p.lower()
                if len(p) > 1 and p not in STOPWORDS:
                    out.append(_fold(p))
    return out


def token_id(token: str) -> int:
    """Stable 31-bit id for a token (sparse-vector index). Collisions are negligible."""
    return zlib.crc32(token.encode("utf-8")) & 0x7FFFFFFF


def doc_vector(tokens: list[str], avgdl: float) -> tuple[list[int], list[float]]:
    """The document side of BM25 for one chunk: (indices, weights)."""
    if not tokens:
        return [], []
    tf = Counter(tokens)
    dl = len(tokens)
    norm = K1 * (1 - B + B * dl / max(avgdl, 1.0))
    weights: dict[int, float] = {}
    for tok, n in tf.items():
        idx = token_id(tok)
        weights[idx] = weights.get(idx, 0.0) + n * (K1 + 1) / (n + norm)
    indices = sorted(weights)
    return indices, [round(weights[i], 6) for i in indices]


def query_vector(text: str) -> tuple[list[int], list[float]]:
    """The query side: weight 1 per distinct query token (idf is applied by Qdrant)."""
    ids = sorted({token_id(t) for t in tokenize(text)})
    return ids, [1.0] * len(ids)
