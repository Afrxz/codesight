# Eval run 20261020-150144

50 questions (45 answerable) on TCG snapshot (83 files). 2026-10-20T15:01:44+00:00

## Retrieval (answerable questions)

| Configuration | Chunks | R@1 | R@3 | R@5 | R@10 | MRR@10 | Ctx precision@5 | Gold coverage@5 | p50 ms |
|---|---|---|---|---|---|---|---|---|---|
| Window · bge-small · dense | 401 | 37.8% | 68.9% | 80.0% | 86.7% | 0.554 | 8.2% | 59.0% | 5.7 |
| Window · bge-small · BM25 | 401 | 40.0% | 60.0% | 71.1% | 88.9% | 0.544 | 7.7% | 55.7% | 15.2 |
| Window · bge-small · hybrid (RRF) | 401 | 48.9% | 75.6% | 84.4% | 93.3% | 0.640 | 7.8% | 64.1% | 20.6 |
| Window · bge-small · hybrid (RRF) + rerank | 401 | 46.7% | 66.7% | 80.0% | 91.1% | 0.600 | 7.7% | 57.0% | 1011.5 |
| AST + merge · bge-small · dense | 560 | 42.2% | 71.1% | 73.3% | 84.4% | 0.568 | 11.9% | 55.5% | 8.5 |
| AST + merge · bge-small · hybrid (RRF) | 560 | 40.0% | 66.7% | 71.1% | 88.9% | 0.559 | 9.7% | 51.9% | 20.5 |
| AST · bge-small · dense | 912 | 42.2% | 71.1% | 77.8% | 84.4% | 0.573 | 14.4% | 54.4% | 5.7 |
| AST · bge-small · BM25 | 912 | 46.7% | 66.7% | 73.3% | 80.0% | 0.588 | 13.2% | 54.1% | 16.2 |
| AST · bge-small · hybrid (RRF) | 912 | 46.7% | 68.9% | 73.3% | 86.7% | 0.600 | 12.9% | 56.1% | 24.3 |
| AST · bge-small · hybrid (RRF) + rerank | 912 | 55.6% | 71.1% | 75.6% | 82.2% | 0.647 | 11.8% | 57.9% | 989.0 |
| AST · jina-code · dense | 912 | 46.7% | 66.7% | 68.9% | 84.4% | 0.578 | 18.3% | 52.2% | 15.8 |
| AST · jina-code · hybrid (RRF) | 912 | 60.0% | 68.9% | 73.3% | 91.1% | 0.675 | 14.8% | 57.4% | 31.3 |
| AST · jina-code · hybrid (RRF) + rerank | 912 | 55.6% | 71.1% | 80.0% | 86.7% | 0.657 | 11.6% | 56.4% | 1009.9 |

## Answers: AST · bge-small · hybrid (RRF) + rerank (DeepSeek · deepseek-chat)

- Key-fact coverage: 92.2%; all key facts: 84.4%
- Cites a gold excerpt: 77.8%; citations valid: 99.2%
- Unanswerable questions correctly declined: 100.0%; answerable questions wrongly declined: 2.2%; answered with a caveat about missing code: 24.4%
- Median time per answer: 3589.5 ms; errors: 0
- LLM judge (1-5, separate metric): correctness 4.73, grounded 4.96 (45 judged)

## Answers: AST · bge-small · hybrid (RRF) (DeepSeek · deepseek-chat)

- Key-fact coverage: 96.3%; all key facts: 91.1%
- Cites a gold excerpt: 75.6%; citations valid: 99.5%
- Unanswerable questions correctly declined: 100.0%; answerable questions wrongly declined: 0.0%; answered with a caveat about missing code: 22.2%
- Median time per answer: 2187.0 ms; errors: 0
- LLM judge (1-5, separate metric): correctness 4.73, grounded 4.91 (45 judged)

## Answers: Window · bge-small · hybrid (RRF) + rerank (DeepSeek · deepseek-chat)

- Key-fact coverage: 95.9%; all key facts: 91.1%
- Cites a gold excerpt: 84.4%; citations valid: 99.0%
- Unanswerable questions correctly declined: 100.0%; answerable questions wrongly declined: 2.2%; answered with a caveat about missing code: 13.3%
- Median time per answer: 3823.0 ms; errors: 0
- LLM judge (1-5, separate metric): correctness 4.80, grounded 4.93 (45 judged)

## Answers: Window · bge-small · hybrid (RRF) (DeepSeek · deepseek-chat)

- Key-fact coverage: 98.9%; all key facts: 97.8%
- Cites a gold excerpt: 91.1%; citations valid: 99.0%
- Unanswerable questions correctly declined: 100.0%; answerable questions wrongly declined: 0.0%; answered with a caveat about missing code: 22.2%
- Median time per answer: 2222.5 ms; errors: 0
- LLM judge (1-5, separate metric): correctness 4.98, grounded 5.00 (45 judged)
