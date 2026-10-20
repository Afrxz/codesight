# Eval run 20261020-143058

50 questions (45 answerable) on TCG snapshot (83 files). 2026-10-20T14:30:58+00:00

## Retrieval (answerable questions)

| Configuration | Chunks | R@1 | R@3 | R@5 | R@10 | MRR@10 | Ctx precision@5 | Gold coverage@5 | p50 ms |
|---|---|---|---|---|---|---|---|---|---|
| Window · bge-small · dense | 401 | 37.8% | 68.9% | 80.0% | 86.7% | 0.554 | 8.2% | 59.0% | 5.4 |
| Window · bge-small · BM25 | 401 | 40.0% | 60.0% | 71.1% | 88.9% | 0.544 | 7.7% | 55.7% | 13.3 |
| Window · bge-small · hybrid (RRF) | 401 | 48.9% | 75.6% | 84.4% | 93.3% | 0.640 | 7.8% | 64.1% | 20.2 |
| Window · bge-small · hybrid (RRF) + rerank | 401 | 46.7% | 66.7% | 80.0% | 91.1% | 0.600 | 7.7% | 57.0% | 978.2 |
| AST (no merge) · bge-small · dense | 912 | 42.2% | 71.1% | 77.8% | 84.4% | 0.573 | 14.4% | 54.4% | 11.3 |
| AST (no merge) · bge-small · hybrid (RRF) | 912 | 46.7% | 68.9% | 73.3% | 86.7% | 0.600 | 12.9% | 56.1% | 22.0 |
| AST · bge-small · dense | 560 | 42.2% | 71.1% | 73.3% | 84.4% | 0.568 | 11.9% | 55.5% | 5.6 |
| AST · bge-small · BM25 | 560 | 42.2% | 60.0% | 71.1% | 77.8% | 0.543 | 10.0% | 50.7% | 13.2 |
| AST · bge-small · hybrid (RRF) | 560 | 40.0% | 66.7% | 71.1% | 88.9% | 0.559 | 9.7% | 51.9% | 20.3 |
| AST · bge-small · hybrid (RRF) + rerank | 560 | 46.7% | 64.4% | 71.1% | 82.2% | 0.581 | 9.7% | 52.5% | 980.3 |
