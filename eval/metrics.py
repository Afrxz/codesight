"""Scoring functions for the eval (pure, so they're unit-tested on their own).

Retrieval
    match        a retrieved chunk "is" gold evidence when it's in the same file and
                 the overlap covers at least half of the shorter of the two
                 (chunk vs gold span). Symmetric on purpose: a 60-line window that
                 contains a whole 10-line gold function counts, and so does a method
                 chunk inside a gold class; a chunk that only grazes the gold doesn't.
    recall@k     share of questions with at least one matching chunk in the top k
    MRR@10       mean of 1 / rank of the first matching chunk (0 if none in the top 10)
    context precision@5
                 share of the lines in the top-5 chunks that are inside gold spans:
                 how much of what the LLM reads is actually relevant
    gold coverage@5
                 share of the gold lines that appear in the top-5 chunks

Generation
    key-fact coverage   per answer, the share of key facts present (each fact is a
                        list of acceptable phrasings, matched case-insensitively)
    declined            the answer OPENS by saying the code doesn't contain the answer
                        (the prompt asks for the direct answer first, or a plain one-
                        sentence "not in the code"), i.e. it refused to answer
    caveat              it answered, but somewhere flags a missing piece ("the excerpts
                        don't show X"): honest, and a sign retrieval missed some evidence
"""

from __future__ import annotations

import re
import statistics

MIN_OVERLAP = 0.5

ABSTAIN = re.compile(
    r"(do(es)?n['’]t|do(es)? not|don['’]t) (contain|include|cover|mention|show|have|appear|describe|define|"
    r"discuss|reference)|"
    r"not (in|covered by|present in|found in|shown in|mentioned in) the (excerpts|code|provided)|"
    r"no (mention|information|evidence|reference|indication)|"
    r"(couldn['’]t|could not|can['’]t|cannot|unable to) (find|see|answer|determine|locate)|"
    r"(isn['’]t|is not|aren['’]t|are not) (covered|included|present|mentioned|shown|in the)|"
    r"there (is|are) no\b", re.IGNORECASE)


def overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start) + 1)


def is_match(hit: dict, gold) -> bool:
    """Does one retrieved chunk count as one of the gold spans?"""
    for g in gold:
        if g.path != hit["path"]:
            continue
        ov = overlap(hit["start_line"], hit["end_line"], g.start, g.end)
        shorter = min(len(g), hit["end_line"] - hit["start_line"] + 1)
        if ov and ov / shorter >= MIN_OVERLAP:
            return True
    return False


def first_match_rank(hits: list[dict], gold) -> int | None:
    return next((i for i, h in enumerate(hits, 1) if is_match(h, gold)), None)


def _lines(hits: list[dict]) -> set[tuple[str, int]]:
    return {(h["path"], n) for h in hits for n in range(h["start_line"], h["end_line"] + 1)}


def context_precision(hits: list[dict], gold, k: int = 5) -> float:
    retrieved = _lines(hits[:k])
    if not retrieved:
        return 0.0
    gold_lines = {(g.path, n) for g in gold for n in range(g.start, g.end + 1)}
    return len(retrieved & gold_lines) / len(retrieved)


def gold_coverage(hits: list[dict], gold, k: int = 5) -> float:
    gold_lines = {(g.path, n) for g in gold for n in range(g.start, g.end + 1)}
    return len(_lines(hits[:k]) & gold_lines) / len(gold_lines) if gold_lines else 0.0


def key_fact_coverage(answer: str, key_facts: list[list[str]]) -> float:
    if not key_facts:
        return 1.0
    text = answer.lower()
    return sum(any(alt.lower() in text for alt in alts) for alts in key_facts) / len(key_facts)


def first_sentence(answer: str) -> str:
    text = (answer or "").strip()
    match = re.search(r"(?<=[.!?])\s", text)
    return text[:match.start()] if match else text


def declined(answer: str) -> bool:
    """It refused: the opening sentence says the answer isn't in the code."""
    return bool(ABSTAIN.search(first_sentence(answer)))


def caveat(answer: str) -> bool:
    """It answered but flagged a gap later on."""
    return not declined(answer) and bool(ABSTAIN.search(answer or ""))


def percentile(values: list[float], p: float) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1], 1)


def mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None
