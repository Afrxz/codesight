# Evaluation results

All numbers here are measured. They come from
`eval/results/20261020-143435/` (`summary.json`, `per_question.jsonl`), and the
"before" column from `eval/results/20261020-141906/`. Nothing is estimated or
rounded up.

## Setup

- **Corpus:** a frozen snapshot of the TCG Portfolio & Market Agent: 83 indexed
  files (61 Python, 1 JavaScript, 21 text/Markdown/JSON), ~700 KB.
- **Questions:** 50 in `eval/qa.jsonl`:
  - 45 answerable, across lookup, how, why, behaviour and config questions;
  - 5 unanswerable, about features the code doesn't have (OAuth, Kubernetes, GraphQL, Redis, iOS).
- **Gold evidence:** every answerable question names the code that answers
  it: a function, class or method, or explicit lines.
  - Symbols are resolved with Python's own `ast` module, **not** with the chunker
    being measured, so the ground truth can't favour either chunker.
  - Every key fact was checked to appear in its gold code (`python -m eval.gold`).
- **A retrieved chunk "matches"** when it's in the gold file and the overlap
  covers ≥ 50% of the shorter of the two. That's symmetric on purpose: a 60-line
  window containing a whole 10-line gold function counts; a chunk that grazes it
  doesn't.
- **Metrics:**
  - **Recall@k:** share of questions with a matching chunk in the top k.
  - **MRR@10:** mean of 1/rank of the first match.
  - **Context precision@5:** share of the lines in the top 5 chunks that are inside gold code, i.e. how much of what the LLM reads is relevant.
  - **Key-fact coverage:** each answer is checked for 2–3 required facts, each a list of acceptable phrasings, matched case-insensitively.
  - **Declined:** the answer's opening sentence says the code doesn't contain the answer.
- **Fixed settings:**
  - 30 candidates per search, RRF k = 60, 20 re-rank candidates;
  - 60-line windows with 15 lines overlap;
  - the LLM gets the top 6 chunks; DeepSeek `deepseek-chat` at temperature 0.
- **Reproducible:** retrieval is deterministic. Three separate runs gave
  identical retrieval numbers.

## Retrieval (45 answerable questions)

| Configuration | Chunks | R@1 | R@3 | R@5 | R@10 | MRR@10 | Context precision@5 |
|---|---|---|---|---|---|---|---|
| Window · bge-small · dense | 401 | 37.8% | 68.9% | 80.0% | 86.7% | 0.554 | 8.2% |
| Window · bge-small · BM25 | 401 | 40.0% | 60.0% | 71.1% | 88.9% | 0.544 | 7.7% |
| **Window · bge-small · hybrid (RRF)** | 401 | 48.9% | **75.6%** | **84.4%** | **93.3%** | 0.640 | 7.8% |
| Window · bge-small · hybrid + rerank | 401 | 46.7% | 66.7% | 80.0% | 91.1% | 0.600 | 7.7% |
| AST · bge-small · dense | 912 | 42.2% | 71.1% | 77.8% | 84.4% | 0.573 | 14.4% |
| AST · bge-small · BM25 | 912 | 46.7% | 66.7% | 73.3% | 80.0% | 0.588 | 13.2% |
| AST · bge-small · hybrid (RRF) | 912 | 46.7% | 68.9% | 73.3% | 86.7% | 0.600 | 12.9% |
| AST · bge-small · hybrid + rerank | 912 | 55.6% | 71.1% | 75.6% | 82.2% | 0.647 | 11.8% |
| AST · jina-code · dense | 912 | 46.7% | 66.7% | 68.9% | 84.4% | 0.578 | **18.3%** |
| **AST · jina-code · hybrid (RRF)** | 912 | **60.0%** | 68.9% | 73.3% | 91.1% | **0.675** | 14.8% |
| AST · jina-code · hybrid + rerank | 912 | 55.6% | 71.1% | 80.0% | 86.7% | 0.657 | 11.6% |
| AST + merge · bge-small · dense *(rejected)* | 560 | 42.2% | 71.1% | 73.3% | 84.4% | 0.568 | 11.9% |
| AST + merge · bge-small · hybrid *(rejected)* | 560 | 40.0% | 66.7% | 71.1% | 88.9% | 0.559 | 9.7% |

## Answers (DeepSeek, all 50 questions)

| Retrieval used | Key-fact coverage | All key facts | Cites gold code | Declines unanswerable | Wrongly declines | LLM judge: correct / grounded (1–5) |
|---|---|---|---|---|---|---|
| **Window · hybrid** | **98.9%** | **97.8%** | **91.1%** | 100% | 0.0% | **4.98 / 5.00** |
| Window · hybrid + rerank | 96.7% | 93.3% | 84.4% | 100% | 0.0% | 4.73 / 4.89 |
| AST · hybrid | 96.3% | 91.1% | 75.6% | 100% | 2.2% | 4.73 / 4.89 |
| AST · hybrid + rerank | 92.2% | 84.4% | 77.8% | 100% | 2.2% | 4.78 / 4.98 |

- Citations pointed at a real excerpt 99.2–99.5% of the time; there were 0 errors in 200 answers.
- **Repeat run** (`eval/results/20261020-150144`, on the final code; it's the
  one the app's Eval page shows):
  - retrieval numbers were identical;
  - three of the four answer setups scored identically;
  - "Window · hybrid + rerank" moved by one question (all key facts 93.3% → 91.1%).
- The LLM judge is DeepSeek grading DeepSeek against the gold code, so treat it
  as a secondary signal. The deterministic key-fact checks are the primary
  metric.

## What the numbers say

1. **Hybrid search beats either search alone.**
   - With windows: Recall@5 84.4% vs 80.0% (dense) and 71.1% (BM25); MRR 0.640 vs 0.554 / 0.544.
   - With AST chunks, hybrid improves the top ranks over dense (MRR 0.600 vs 0.573) but not Recall@5 (73.3% vs 77.8%).
2. **AST chunking is more precise, but on this corpus it finds less.**
   - What the LLM reads is ~1.7× more relevant: context precision@5 is 12.9% vs 7.8% (hybrid) and 14.4% vs 8.2% (dense).
   - But Recall@5 is lower (73.3% vs 84.4%, hybrid), and answers are slightly worse (91.1% vs 97.8% fully correct, i.e. 41 vs 44 of 45 questions).
   - **Why:** this codebase documents each mechanism in long module docstrings. As separate chunks, those docstrings often outrank the implementation the gold points to. A 60-line window bundles the docstring *and* the first functions, so it counts as a hit.
3. **The ms-marco cross-encoder helps the #1 result but not the answers.**
   - On AST chunks it lifts Recall@1 from 46.7% to 55.6% and MRR from 0.600 to 0.647.
   - But it lowers Recall@10, makes answers worse with both chunkers, and adds about 4–5 s per question on this CPU.
   - The likely reason: ms-marco-MiniLM is trained on web search passages, not code.
4. **The code-specific embedding model gives the best top results.**
   - jina-code + hybrid has the best Recall@1 (60.0%) and MRR (0.675), and the highest context precision (18.3% dense).
   - It's no better at Recall@5, and it's 6.5× slower to index (155 s vs 24 s for the same 912 chunks).
5. **Tried, measured, rejected: merging small neighbouring chunks.** It lowered recall and precision (hybrid: R@5 71.1% vs 73.3%, precision 9.7% vs 12.9%). It remains an eval variant only.
6. **Attaching comments to the definition below them** (v1 → current chunker):
   - small gains in ranking: hybrid + rerank R@1 51.1% → 55.6%, MRR 0.620 → 0.647; dense MRR 0.555 → 0.573;
   - one loss: dense R@5 80.0% → 77.8%.

   It's kept because it removes noise chunks that held only a section comment.
7. **Answers are grounded.** All four setups declined every unanswerable
   question. Wrongly declining a real question happened in at most 1 of 45.

## Claims these numbers support

- AST-aware chunking: ~1.7× higher context precision than 60-line fixed windows
  (12.9% vs 7.8% of retrieved lines relevant, top-5, hybrid).
- Hybrid dense + BM25 with RRF: Recall@5 84.4% and MRR 0.640, vs 80.0% / 0.554
  dense-only (windows).
- Cross-encoder re-ranking: Recall@1 46.7% → 55.6% and MRR 0.600 → 0.647 on AST
  chunks. It did **not** improve final answer quality on this suite.
- End to end, the best setup answered 97.8% of the 45 answerable questions with
  every key fact, cited the gold code in 91.1%, and declined 5/5 unanswerable
  ones.

## Limitations

- **One corpus.** It's mostly Python with heavy docstrings, so results may differ on other codebases.
- **45 answerable questions,** so each question is 2.2 points; small differences
  (e.g. 96.3% vs 98.9%) are within one or two questions.
- **The gold is strict:** it's the implementing code. A module docstring that
  explains a mechanism isn't counted, which partly penalizes AST chunks (see point 2).
- **Key facts are matched as strings,** so a correct answer phrased unusually can be scored as missing a fact.
- **Latency was measured while other programs** (and, at times, other indexing
  jobs) were using the CPU, so treat the timings as upper bounds. README section 7
  has clean measurements.

## Reproduce

```bash
python -m eval.gold                              # check the gold data
python -m eval.run                               # retrieval for all configurations (no API key)
python -m eval.run --generation --judge          # + answers and judge (needs an LLM key)
python -m eval.run --rescore <run_id>            # recompute a saved run's answer metrics
```
