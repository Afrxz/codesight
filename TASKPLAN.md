# RAGG — Codebase Q&A Engine: Task Plan

Ask questions about a codebase in plain English and get answers grounded in the
actual code, with clickable citations to the exact files and lines.

Target (from the CV description):
- End-to-end RAG pipeline: **FastAPI** + **Qdrant**.
- **AST-aware chunking** via tree-sitter (function/class boundaries), measured
  against a **fixed-window baseline**.
- **Hybrid retrieval**: dense vectors + **BM25** sparse search, fused with
  **Reciprocal Rank Fusion**, re-ranked by a **cross-encoder** (ms-marco-MiniLM).
- **LLM abstraction** with **LiteLLM**: switch DeepSeek / Groq / OpenAI / Gemini at
  runtime without touching downstream code. DeepSeek is the default and the one
  tested live.
- **Evaluation harness**: 50 domain-specific Q&A pairs; retrieval and generation
  quality measured, architecture choices backed by data.
- A proper UI (no Streamlit) with a Settings panel, like the TCG binder.

## Decisions (agreed with the user)
- Python 3.14, own `.venv`. No Docker on this machine → **Qdrant embedded mode**
  (a local folder) by default; a Qdrant server URL is a setting.
- **Local embeddings** (fastembed / ONNX, CPU): `BAAI/bge-small-en-v1.5` default;
  `jinaai/jina-embeddings-v2-base-code` as a measured alternative. Must run on an
  ordinary laptop: no GPU dependency, batch size setting, reranker optional.
- **Eval corpus**: a frozen snapshot of the TCG project's code.
- No orchestration/RAG framework (no LangChain/LlamaIndex): the pipeline is
  plain, explainable code. LiteLLM is the one abstraction library, by design.
- Only **measured** numbers go into RESULTS.md / the README / the CV.
- Build straight through; report at the end.

## Phases

### 1. Foundation
- Project layout, settings registry (`.env`, validated, editable from the UI),
  logging, `.gitignore`, `.env.example`.
- **Acceptance:** settings load with defaults; invalid values are rejected.

### 2. Loading + chunking
- Repo loader: local folder (or a git URL, shallow-cloned), respects
  `.gitignore` and skips vendored/binary/huge files.
- AST chunker (tree-sitter): one chunk per function/method/class, large classes
  split per method with the class signature kept as context, module-level code
  grouped; metadata = path, language, start/end line, qualified symbol, kind.
  Languages: Python, JavaScript, TypeScript (+ others the language pack covers
  via a generic node list). Unsupported files fall back to the window chunker.
- Fixed-window chunker (N lines, overlap) as the baseline.
- **Acceptance:** unit tests show functions/classes are never split mid-body,
  line numbers are exact, and the window chunker covers every line.

### 3. Indexing (Qdrant)
- Dense embeddings (fastembed) + BM25 sparse vectors (code-aware tokenizer:
  splits snake_case/camelCase, keeps whole identifiers) stored as named vectors
  in one Qdrant collection per repo+config; Qdrant's IDF modifier supplies BM25's
  idf term.
- Incremental re-index by file hash; background job with progress.
- **Acceptance:** index a repo, re-index after a change touches only that file.

### 4. Retrieval
- Dense search, sparse (BM25) search, **RRF fusion** (implemented in Python),
  **cross-encoder rerank**; every stage's rank/score and timing returned.
- **Acceptance:** tests on a tiny fixture repo: exact-identifier questions are
  found by BM25, paraphrased ones by dense, RRF math verified by hand.

### 5. Generation (LiteLLM)
- Provider-agnostic `llm.py`: DeepSeek, Groq, OpenAI, Gemini; streaming; key
  test. Prompt: numbered context with path:lines; answer only from context; cite
  [n]; say when the code doesn't contain the answer.
- **Acceptance:** live DeepSeek answer with valid citations; provider switch is a
  setting change only (unit-tested with a fake completion function).

### 6. API + UI
- FastAPI: repos (add/index/re-index/delete, progress), ask (server-sent events:
  sources → tokens → done), source viewer (path-traversal safe), settings
  (+ key test), eval results.
- UI: repo sidebar with indexing progress; chat with streamed, rendered answers;
  clickable citations open the code at the cited lines; "how it was found" panel
  (dense / BM25 / RRF / rerank ranks + timings); Settings dialog; Eval page.
- **Acceptance:** end-to-end in the browser on a real repo.

### 7. Evaluation harness
- `eval/corpus/tcg/` frozen snapshot; `eval/qa.jsonl` 50 pairs (question, gold
  file+symbol evidence, key facts, category).
- Retrieval metrics: Recall@k, MRR, context precision (share of retrieved lines
  inside gold spans), latency — for chunker {AST, window} × retrieval
  {dense, BM25, hybrid, hybrid+rerank} × embedding model.
- Generation metrics (DeepSeek): key-fact coverage (deterministic), citation
  accuracy, optional LLM judge as a separate metric.
- `python -m eval.run` → `eval/results/<run_id>/`; measured write-up in
  `eval/RESULTS.md`.
- **Acceptance:** reproducible retrieval numbers (deterministic) across two runs.

### 8. Docs + final verification
- README: install, use every feature, settings, architecture, eval results,
  troubleshooting, laptop performance (measured).
- Full test suite; fresh-start walkthrough.
