"""The eval's scoring functions and gold data (the numbers in RESULTS.md depend on these)."""

from eval import gold, metrics
from eval.gold import Span


def hit(path, a, b):
    return {"path": path, "start_line": a, "end_line": b}


G = [Span("src/run.py", 100, 109)]          # a 10-line gold function


def test_match_needs_half_of_the_shorter_side():
    assert metrics.is_match(hit("src/run.py", 100, 109), G)             # exact
    assert metrics.is_match(hit("src/run.py", 80, 139), G)              # a window containing it
    assert metrics.is_match(hit("src/run.py", 104, 106), G)             # a small chunk inside it
    assert not metrics.is_match(hit("src/run.py", 107, 160), G)         # grazes it: 3 of 10 lines
    assert not metrics.is_match(hit("src/other.py", 100, 109), G)       # another file


def test_rank_precision_and_coverage():
    hits = [hit("a.py", 1, 10), hit("src/run.py", 95, 104), hit("src/run.py", 100, 109)]
    assert metrics.first_match_rank(hits, G) == 2                        # 5 of 10 gold lines = half
    # Lines retrieved (top 5): 10 + 15 (95..109, de-duplicated) = 25; of those, 10 are gold.
    assert metrics.context_precision(hits, G) == 10 / 25
    assert metrics.gold_coverage(hits, G) == 1.0
    assert metrics.first_match_rank([hit("a.py", 1, 5)], G) is None


def test_key_facts_and_abstention():
    facts = [["3", "three"], ["backoff", "exponential"]]
    assert metrics.key_fact_coverage("It tries three times with exponential backoff.", facts) == 1.0
    assert metrics.key_fact_coverage("It tries 3 times.", facts) == 0.5
    assert metrics.declined("The excerpts don't contain any information about OAuth.")
    assert metrics.declined("I couldn't find a Kubernetes manifest in the provided code. Try searching.")
    assert not metrics.declined("`fetch_with_retry` retries 3 times [1].")
    partial = "It retries 3 times [1]. The excerpts don't show where the constant is set."
    assert not metrics.declined(partial) and metrics.caveat(partial)


def test_percentile():
    assert metrics.percentile([5, 1, 3], 50) == 3
    assert metrics.percentile([], 50) is None


def test_the_suite_is_complete_and_resolvable():
    questions = gold.load()
    assert len(questions) == 50 and sum(q.answerable for q in questions) == 45
    for q in questions:
        if q.answerable:
            assert q.gold and q.key_facts
            text = "\n".join(gold.span_text(s) for s in q.gold).lower()
            assert all(any(a.lower() in text for a in alts) for alts in q.key_facts), q.id
