# Eval run 20261020-142826

50 questions (45 answerable) on TCG snapshot (83 files). 2026-10-20T14:28:26+00:00

## Retrieval (answerable questions)

| Configuration | Chunks | R@1 | R@3 | R@5 | R@10 | MRR@10 | Ctx precision@5 | Gold coverage@5 | p50 ms |
|---|---|---|---|---|---|---|---|---|---|
| Window · bge-small · dense | 401 | 37.8% | 68.9% | 80.0% | 86.7% | 0.554 | 8.2% | 59.0% | 5.6 |
| Window · bge-small · BM25 | 401 | 40.0% | 60.0% | 71.1% | 88.9% | 0.544 | 7.7% | 55.7% | 14.6 |
| Window · bge-small · hybrid (RRF) | 401 | 48.9% | 75.6% | 84.4% | 93.3% | 0.640 | 7.8% | 64.1% | 20.4 |
| Window · bge-small · hybrid (RRF) + rerank | 401 | 46.7% | 66.7% | 80.0% | 91.1% | 0.600 | 7.7% | 57.0% | 986.2 |
| AST (no merge) · bge-small · dense | 912 | 42.2% | 71.1% | 77.8% | 84.4% | 0.573 | 14.4% | 54.4% | 7.7 |
| AST (no merge) · bge-small · hybrid (RRF) | 912 | 46.7% | 68.9% | 73.3% | 86.7% | 0.600 | 12.9% | 56.1% | 23.3 |
| AST · bge-small · dense | 996 | 40.0% | 71.1% | 80.0% | 84.4% | 0.555 | 15.0% | 56.6% | 5.6 |
| AST · bge-small · BM25 | 996 | 44.4% | 64.4% | 73.3% | 82.2% | 0.572 | 13.6% | 54.1% | 17.1 |
| AST · bge-small · hybrid (RRF) | 996 | 46.7% | 66.7% | 73.3% | 86.7% | 0.589 | 12.9% | 56.0% | 24.0 |
| AST · bge-small · hybrid (RRF) + rerank | 996 | 51.1% | 68.9% | 73.3% | 82.2% | 0.620 | 11.9% | 56.3% | 997.3 |
