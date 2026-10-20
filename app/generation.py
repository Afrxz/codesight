"""Answering: retrieved chunks + question -> a grounded, cited answer.

The prompt gives the model numbered excerpts ([1] path:lines, symbol, code)
and three rules: answer only from the excerpts, cite every claim with [n], and
say plainly when the excerpts don't contain the answer (instead of guessing).

`ask()` yields events, so the API can stream them to the browser:
    {"type": "retrieval", ...}   the sources, found before any text is written
    {"type": "token", "text"}    the answer as it's generated
    {"type": "done", ...}        final answer, which sources were cited, timings
    {"type": "error", "message"}
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator

from . import config, llm
from .retrieval import Hit, retrieve

SYSTEM_PROMPT = """\
You are a senior engineer answering questions about the codebase "{repo}".
You are given numbered excerpts from it, retrieved for this question.

Rules:
- Answer ONLY from the excerpts. Don't use outside knowledge about this codebase.
- Cite the excerpt(s) behind every claim like [1] or [2][3], right after the claim.
- If the excerpts don't contain the answer, say so plainly in one sentence and
  suggest what to look for. Never guess at code you can't see.
- Write identifiers exactly as they appear, in backticks. Include a short code
  snippet only when it makes the answer clearer.
- Be concise: lead with the direct answer, then the key details.
"""

CITATION = re.compile(r"\[(\d{1,2})\]")
FOLLOW_UP_WORDS = 8            # a short follow-up borrows the previous question for retrieval


def format_context(hits: list[Hit]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        title = f"[{i}] {h.path}:{h.start_line}-{h.end_line}" + (f" ({h.symbol})" if h.symbol else "")
        context = f"{h.context}\n" if h.context else ""
        blocks.append(f"{title}\n```{h.language or ''}\n{context}{h.text}\n```")
    return "\n\n".join(blocks)


def build_messages(repo_name: str, question: str, hits: list[Hit], history: list[dict] | None = None) -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(repo=repo_name)}]
    for turn in (history or [])[-3:]:                  # recent turns, for follow-up questions
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})
    messages.append({"role": "user", "content": f"Question: {question}\n\nCode excerpts:\n\n{format_context(hits)}"})
    return messages


def cited(answer: str, n_hits: int) -> list[int]:
    """Excerpt numbers the answer cites (valid ones only), in first-cited order."""
    seen: list[int] = []
    for m in CITATION.finditer(answer):
        n = int(m.group(1))
        if 1 <= n <= n_hits and n not in seen:
            seen.append(n)
    return seen


def search_query(question: str, history: list[dict] | None) -> str:
    """'and how is it tested?' alone retrieves nothing useful; add the previous question."""
    if history and len(question.split()) < FOLLOW_UP_WORDS:
        return f"{history[-1]['question']} {question}"
    return question


def ask(repo: dict, question: str, history: list[dict] | None = None, **retrieval_options) -> Iterator[dict]:
    """The whole pipeline for one question, as a stream of events."""
    question = question.strip()
    if not question:
        yield {"type": "error", "message": "Ask a question first."}
        return
    try:
        t = llm.target()                               # fail fast with a clear message if no key
    except config.ConfigError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    started = time.perf_counter()
    result = retrieve(repo, search_query(question, history), **retrieval_options)
    yield {"type": "retrieval", "hits": [h.view() for h in result.hits], "mode": result.mode,
           "reranked": result.reranked, "timings_ms": result.timings_ms, "candidates": result.candidates}
    if not result.hits:
        yield {"type": "done", "answer": "I couldn't find any code related to that question in this repo.",
               "cited": [], "model": t.label, "timings_ms": result.timings_ms}
        return

    messages = build_messages(repo["name"], question, result.hits, history)
    gen_started = time.perf_counter()
    first_token_ms = None
    parts: list[str] = []
    try:
        for piece in llm.stream(messages, t=t):
            if first_token_ms is None:
                first_token_ms = round((time.perf_counter() - gen_started) * 1000)
            parts.append(piece)
            yield {"type": "token", "text": piece}
    except Exception as exc:
        yield {"type": "error", "message": f"{t.label} failed: {type(exc).__name__}: {str(exc)[:300]}"}
        return
    answer = "".join(parts)
    timings = dict(result.timings_ms, first_token_ms=first_token_ms,
                   generate_ms=round((time.perf_counter() - gen_started) * 1000),
                   overall_ms=round((time.perf_counter() - started) * 1000))
    yield {"type": "done", "answer": answer, "cited": cited(answer, len(result.hits)), "model": t.label,
           "timings_ms": timings}


def answer_once(repo: dict, question: str, **retrieval_options) -> dict:
    """Non-streaming version (for the eval): the final answer plus its sources."""
    out: dict = {"answer": "", "hits": [], "cited": [], "error": None}
    for event in ask(repo, question, **retrieval_options):
        if event["type"] == "retrieval":
            out["hits"] = event["hits"]
            out["retrieval_ms"] = event["timings_ms"]
        elif event["type"] == "done":
            out.update(answer=event["answer"], cited=event["cited"], model=event["model"],
                       timings_ms=event["timings_ms"])
        elif event["type"] == "error":
            out["error"] = event["message"]
    return out
