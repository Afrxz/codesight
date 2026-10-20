"""The eval's ground truth: questions + where their answers live in the corpus.

Each question in qa.jsonl names its gold evidence as
    {"path": "src/run.py", "symbol": "fetch_with_retry"}   (a function/class/method)
    {"path": "src/db.py", "lines": [132, 143]}             (explicit lines)
Symbols are resolved to line ranges with Python's own `ast` module, NOT with
RAGG's tree-sitter chunker, so the ground truth is independent of the chunking
being measured (decorators included, like a reader would include them).

    python -m eval.gold        # check every gold span and key fact against the corpus
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
CORPUS = EVAL_DIR / "corpus" / "tcg"
QA_FILE = EVAL_DIR / "qa.jsonl"


@dataclass
class Span:
    path: str
    start: int
    end: int

    def __len__(self) -> int:
        return self.end - self.start + 1


@dataclass
class Question:
    id: str
    category: str
    question: str
    gold: list[Span]
    key_facts: list[list[str]]
    answerable: bool = True
    raw_gold: list[dict] = field(default_factory=list)


def symbol_span(path: str, symbol: str) -> Span:
    """Line range of `Name` or `Class.method` in a corpus .py file (decorators included)."""
    tree = ast.parse((CORPUS / path).read_text(encoding="utf-8"))
    scope: list = tree.body
    node = None
    for part in symbol.split("."):
        node = next((n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.name == part), None)
        if node is None:
            raise KeyError(f"{symbol!r} not found in {path}")
        scope = node.body
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return Span(path, start, node.end_lineno)


def load() -> list[Question]:
    questions = []
    for line in QA_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        q = json.loads(line)
        spans = [symbol_span(g["path"], g["symbol"]) if "symbol" in g else Span(g["path"], *g["lines"])
                 for g in q["gold"]]
        questions.append(Question(id=q["id"], category=q["category"], question=q["question"], gold=spans,
                                  key_facts=q["key_facts"], answerable=q.get("answerable", True),
                                  raw_gold=q["gold"]))
    ids = [q.id for q in questions]
    assert len(ids) == len(set(ids)), "duplicate question ids"
    return questions


def span_text(span: Span) -> str:
    lines = (CORPUS / span.path).read_text(encoding="utf-8").split("\n")
    return "\n".join(lines[span.start - 1:span.end])


def main() -> None:
    questions = load()
    problems = 0
    for q in questions:
        text = "\n".join(span_text(s) for s in q.gold).lower()
        missing = [alts for alts in q.key_facts if not any(a.lower() in text for a in alts)]
        flag = "  <-- key fact not in gold code: " + str(missing) if missing else ""
        problems += bool(missing)
        print(f"{q.id} [{q.category}] " + ", ".join(f"{s.path}:{s.start}-{s.end}" for s in q.gold) + flag)
    answerable = sum(q.answerable for q in questions)
    print(f"\n{len(questions)} questions ({answerable} answerable, {len(questions) - answerable} unanswerable); "
          f"{problems} with key facts not found in their gold code")


if __name__ == "__main__":
    main()
