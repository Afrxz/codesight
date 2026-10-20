"""The benchmark: retrieval for every configuration, then (optionally) answers.

    python -m eval.run                       retrieval only: no API key needed, deterministic
    python -m eval.run --generation          + DeepSeek answers for the main configurations
    python -m eval.run --generation --judge  + an LLM judge score (a separate, fuzzier metric)
    python -m eval.run --quick               only the bge-small configurations (skips the
                                             ~640 MB code model)

Everything runs on the frozen corpus (eval/corpus/tcg) and the 50 questions in
eval/qa.jsonl, in its own work folder (eval/.work), so it never touches the
app's own index. Indexing settings are pinned (60-line windows, 15 overlap,
default limits), so your Settings can't change the numbers.

Writes eval/results/<run_id>/: summary.json (shown on the app's Eval page),
per_question.jsonl and report.md.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app import config

EVAL_DIR = Path(__file__).resolve().parent
WORK_DIR = EVAL_DIR / ".work"
RESULTS_DIR = EVAL_DIR / "results"
config.DATA_DIR = WORK_DIR             # before anything opens the index

from app import generation, indexer, llm, store  # noqa: E402
from app.retrieval import retrieve  # noqa: E402

from . import gold as gold_mod  # noqa: E402
from . import metrics as m  # noqa: E402

log = logging.getLogger("ragg.eval")

BGE, JINA = "BAAI/bge-small-en-v1.5", "jinaai/jina-embeddings-v2-base-code"
K_VALUES = (1, 3, 5, 10)
RETRIEVE_K = 10
# (chunker, embedding model, search mode, re-rank)
CONFIGS = [
    ("window", BGE, "dense", False), ("window", BGE, "sparse", False),
    ("window", BGE, "hybrid", False), ("window", BGE, "hybrid", True),
    ("ast-merge", BGE, "dense", False), ("ast-merge", BGE, "hybrid", False),
    ("ast", BGE, "dense", False), ("ast", BGE, "sparse", False),
    ("ast", BGE, "hybrid", False), ("ast", BGE, "hybrid", True),
    ("ast", JINA, "dense", False), ("ast", JINA, "hybrid", False), ("ast", JINA, "hybrid", True),
]
# Answers are generated for hybrid search with and without re-ranking, on both chunkers.
GEN_CONFIGS = [("ast", BGE, "hybrid", True), ("ast", BGE, "hybrid", False),
               ("window", BGE, "hybrid", True), ("window", BGE, "hybrid", False)]
PINNED_INDEXING = {"WINDOW_LINES": "60", "WINDOW_OVERLAP": "15", "MAX_FILE_KB": "300", "EXTRA_IGNORE": "",
                   "EMBED_BATCH": "8"}
SHORT = {BGE: "bge-small", JINA: "jina-code"}


def label(cfg) -> str:
    chunker, model, mode, rerank = cfg
    search = {"dense": "dense", "sparse": "BM25", "hybrid": "hybrid (RRF)"}[mode]
    name = {"ast": "AST", "ast-merge": "AST + merge", "window": "Window"}[chunker]
    return f"{name} · {SHORT[model]} · {search}{' + rerank' if rerank else ''}"


@contextmanager
def pinned(values: dict):
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def build_index(chunker: str, model: str) -> dict:
    """The corpus indexed with one chunker + embedding model (reused when up to date)."""
    repo_id = f"tcg-{chunker}-{SHORT[model]}"
    if store.get_repo(repo_id) is None:
        registry = store.load_registry()
        registry[repo_id] = {"id": repo_id, "name": "tcg", "source": str(gold_mod.CORPUS), "is_git": False,
                             "root": None, "status": "new", "chunker": None, "embed_model": None,
                             "collection": None, "files": {}, "stats": {}, "avgdl": None, "indexed_at": None,
                             "error": None}
        store.save_registry(registry)
    with pinned({**PINNED_INDEXING, "EMBED_MODEL": model}):
        result = indexer.reindex(repo_id, chunker=chunker)
    repo = store.get_repo(repo_id)
    log.info("Index %s: %s chunks (%s, %.0f s)", repo_id, repo["stats"]["chunks"],
             "rebuilt" if result["rebuilt"] else f"{result['changed_files']} changed", result["seconds"])
    repo["_index_seconds"] = result["seconds"] if result["changed_files"] else None
    return repo


def eval_retrieval(repo: dict, cfg, questions) -> tuple[dict, list[dict]]:
    chunker, model, mode, rerank = cfg
    retrieve(repo, "warm up the models", mode=mode, rerank=rerank, top_k=RETRIEVE_K)   # not timed
    rows = []
    for q in questions:
        r = retrieve(repo, q.question, mode=mode, rerank=rerank, top_k=RETRIEVE_K, candidates=30,
                     rerank_candidates=20, rrf_k=60)
        hits = [h.view() for h in r.hits]
        rank = m.first_match_rank(hits, q.gold)
        rows.append({"id": q.id, "rank": rank, "ms": r.timings_ms["total_ms"],
                     "ctx_precision": round(m.context_precision(hits, q.gold), 4),
                     "gold_coverage": round(m.gold_coverage(hits, q.gold), 4),
                     "top5": [f"{h['path']}:{h['start_line']}-{h['end_line']}" for h in hits[:5]]})
    n = len(rows)
    summary = {
        "label": label(cfg), "chunker": chunker, "embed_model": model, "mode": mode, "rerank": rerank,
        "chunks": repo["stats"]["chunks"],
        "recall": {str(k): round(sum(1 for r in rows if r["rank"] and r["rank"] <= k) / n, 4) for k in K_VALUES},
        "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 4),
        "context_precision@5": m.mean(r["ctx_precision"] for r in rows),
        "gold_coverage@5": m.mean(r["gold_coverage"] for r in rows),
        "latency_ms": {"p50": m.percentile([r["ms"] for r in rows], 50),
                       "p90": m.percentile([r["ms"] for r in rows], 90)},
        "by_category": {},
    }
    for cat in sorted({q.category for q in questions}):
        ids = {q.id for q in questions if q.category == cat}
        sub = [r for r in rows if r["id"] in ids]
        summary["by_category"][cat] = round(sum(1 for r in sub if r["rank"] and r["rank"] <= 5) / len(sub), 4)
    return summary, rows


JUDGE_PROMPT = """You grade answers about a codebase. Given the question, the gold code that
answers it, the key facts a correct answer must contain, and the excerpts the
answering model was shown, rate the answer:
- correctness 1-5: does it correctly answer the question, consistent with the gold code?
- grounded 1-5: is every claim supported by the excerpts it was shown (no invented details)?
Reply with JSON only: {"correctness": n, "grounded": n, "why": "one sentence"}"""


def judge(q, answer: str, hits: list[dict]) -> dict | None:
    gold_text = "\n\n".join(f"{s.path}:{s.start}-{s.end}\n{gold_mod.span_text(s)}" for s in q.gold)
    excerpts = "\n\n".join(f"[{i}] {h['path']}:{h['start_line']}-{h['end_line']}\n{h['text']}"
                           for i, h in enumerate(hits, 1))
    user = (f"Question: {q.question}\n\nKey facts: {q.key_facts}\n\nGold code:\n{gold_text}\n\n"
            f"Excerpts shown:\n{excerpts}\n\nAnswer to grade:\n{answer}")
    try:
        out = llm.complete([{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": user}],
                           max_tokens=200, temperature=0)
        text = out["text"]
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
        return {"correctness": float(data["correctness"]), "grounded": float(data["grounded"]),
                "why": str(data.get("why", ""))[:300]}
    except Exception as exc:
        log.warning("Judge failed for %s: %s", q.id, exc)
        return None


def eval_generation(repo: dict, cfg, questions, use_judge: bool) -> tuple[dict, list[dict]]:
    chunker, model, mode, rerank = cfg

    def one(q):
        t = time.perf_counter()
        out = generation.answer_once(repo, q.question, mode=mode, rerank=rerank)
        ms = round((time.perf_counter() - t) * 1000)
        answer, hits = out["answer"], out["hits"]
        raw_cites = [int(n) for n in generation.CITATION.findall(answer)]
        row = {"id": q.id, "answerable": q.answerable, "answer": answer, "error": out["error"], "ms": ms,
               "cited": out["cited"], "raw_citations": raw_cites, "n_hits": len(hits),
               "declined": m.declined(answer), "caveat": m.caveat(answer),
               "key_fact_coverage": m.key_fact_coverage(answer, q.key_facts) if q.answerable else None,
               "cited_gold": any(m.is_match(hits[n - 1], q.gold) for n in out["cited"]) if q.answerable else None,
               "sources": [f"{h['path']}:{h['start_line']}-{h['end_line']}" for h in hits]}
        if use_judge and q.answerable and answer:
            row["judge"] = judge(q, answer, hits)
        return row

    with pinned({"LLM_TEMPERATURE": "0", "TOP_K": "6"}), ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(one, questions))
    return summarize_generation(rows, label(cfg), llm.target().label), rows


def summarize_generation(rows: list[dict], cfg_label: str, model: str) -> dict:
    """Answer metrics from per-question rows (also used to re-score a saved run)."""
    for r in rows:                        # scored from the stored text, so re-scoring is exact
        r["declined"], r["caveat"] = m.declined(r["answer"]), m.caveat(r["answer"])
    ans = [r for r in rows if r["answerable"]]
    unans = [r for r in rows if not r["answerable"]]
    all_cites = [n for r in rows for n in r["raw_citations"]]
    judged = [r["judge"] for r in ans if r.get("judge")]
    return {
        "label": cfg_label, "retrieval_label": cfg_label, "model": model, "questions": len(rows),
        "errors": sum(1 for r in rows if r["error"]),
        "key_fact_coverage": m.mean(r["key_fact_coverage"] for r in ans),
        "fully_correct_rate": round(sum(1 for r in ans if r["key_fact_coverage"] == 1.0) / len(ans), 4),
        "cited_gold_rate": round(sum(1 for r in ans if r["cited_gold"]) / len(ans), 4),
        "citation_validity": round(sum(1 for n in all_cites if 1 <= n <= 6) / len(all_cites), 4) if all_cites else None,
        "abstain_ok": round(sum(1 for r in unans if r["declined"]) / len(unans), 4) if unans else None,
        "false_abstain_rate": round(sum(1 for r in ans if r["declined"]) / len(ans), 4),
        "caveat_rate": round(sum(1 for r in ans if r["caveat"]) / len(ans), 4),
        "latency_ms": {"p50": m.percentile([r["ms"] for r in rows], 50), "p90": m.percentile([r["ms"] for r in rows], 90)},
        "judge": {"correctness": m.mean(j["correctness"] for j in judged), "grounded": m.mean(j["grounded"] for j in judged),
                  "judged": len(judged)} if judged else None,
    }


def report_md(summary: dict) -> str:
    lines = [f"# Eval run {summary['run_id']}", "",
             f"{summary['corpus']['questions']} questions ({summary['corpus']['answerable']} answerable) on "
             f"{summary['corpus']['name']} ({summary['corpus']['files']} files). {summary['created_at']}", "",
             "## Retrieval (answerable questions)", "",
             "| Configuration | Chunks | R@1 | R@3 | R@5 | R@10 | MRR@10 | Ctx precision@5 | Gold coverage@5 | p50 ms |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for c in summary["retrieval"]["configs"]:
        r = c["recall"]
        lines.append(f"| {c['label']} | {c['chunks']} | {r['1']:.1%} | {r['3']:.1%} | {r['5']:.1%} | {r['10']:.1%} | "
                     f"{c['mrr']:.3f} | {c['context_precision@5']:.1%} | {c['gold_coverage@5']:.1%} | {c['latency_ms']['p50']} |")
    for g in summary.get("generation_runs") or []:
        lines += ["", f"## Answers: {g['label']} ({g['model']})", "",
                  f"- Key-fact coverage: {g['key_fact_coverage']:.1%}; all key facts: {g['fully_correct_rate']:.1%}",
                  f"- Cites a gold excerpt: {g['cited_gold_rate']:.1%}; citations valid: "
                  + (f"{g['citation_validity']:.1%}" if g['citation_validity'] is not None else "n/a"),
                  f"- Unanswerable questions correctly declined: "
                  + (f"{g['abstain_ok']:.1%}" if g['abstain_ok'] is not None else "n/a")
                  + f"; answerable questions wrongly declined: {g['false_abstain_rate']:.1%}"
                  + f"; answered with a caveat about missing code: {g.get('caveat_rate', 0):.1%}",
                  f"- Median time per answer: {g['latency_ms']['p50']} ms; errors: {g['errors']}"]
        if g.get("judge"):
            lines.append(f"- LLM judge (1-5, separate metric): correctness {g['judge']['correctness']:.2f}, "
                         f"grounded {g['judge']['grounded']:.2f} ({g['judge']['judged']} judged)")
    return "\n".join(lines) + "\n"


def rescore(out_dir: Path) -> None:
    """Re-derive the answer metrics of a saved run from its stored answers."""
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (out_dir / "per_question.jsonl").read_text(encoding="utf-8").splitlines()]
    runs = []
    for g in summary.get("generation_runs") or []:
        mine = [r for r in rows if r["kind"] == "answer" and r["config"] == g["label"]]
        runs.append({**summarize_generation(mine, g["label"], g["model"]), "latency_ms": g["latency_ms"]})
    summary["generation_runs"], summary["generation"] = runs, (runs[0] if runs else None)
    summary.setdefault("notes", []).append("Answer metrics re-scored from the stored answers "
                                           "(declined = the opening sentence says it's not in the code).")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report = report_md(summary)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the RAGG benchmark.")
    parser.add_argument("--generation", action="store_true", help="also generate answers (needs an LLM key)")
    parser.add_argument("--judge", action="store_true", help="also score answers with an LLM judge")
    parser.add_argument("--quick", action="store_true", help="skip the jina code-embedding configurations")
    parser.add_argument("--rescore", metavar="RUN_ID", help="recompute a saved run's answer metrics (no API calls)")
    parser.add_argument("--limit", type=int, help="only the first N questions (a quick smoke run)")
    args = parser.parse_args(argv)
    if args.rescore:
        rescore(RESULTS_DIR / args.rescore)
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("httpx", "LiteLLM", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    questions = gold_mod.load()
    if args.limit:
        questions = questions[:args.limit] + [q for q in questions if not q.answerable][:1]
    answerable = [q for q in questions if q.answerable]
    configs = [c for c in CONFIGS if not (args.quick and c[1] == JINA)]
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%d-%H%M%S")
    out_dir = RESULTS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    indexes, index_info = {}, {}
    for chunker, model in sorted({(c[0], c[1]) for c in configs + (GEN_CONFIGS if args.generation else [])}):
        indexes[(chunker, model)] = build_index(chunker, model)
        index_info[f"{chunker}/{SHORT[model]}"] = {"chunks": indexes[(chunker, model)]["stats"]["chunks"],
                                                   "index_seconds": indexes[(chunker, model)]["_index_seconds"]}
    per_question = []
    retrieval = []
    for cfg in configs:
        log.info("Retrieval: %s", label(cfg))
        summary, rows = eval_retrieval(indexes[(cfg[0], cfg[1])], cfg, answerable)
        retrieval.append(summary)
        per_question += [{"kind": "retrieval", "config": label(cfg), **r} for r in rows]

    generation_runs = []
    if args.generation:
        for cfg in GEN_CONFIGS:
            log.info("Answers: %s", label(cfg))
            summary, rows = eval_generation(indexes[(cfg[0], cfg[1])], cfg, questions, args.judge)
            generation_runs.append(summary)
            per_question += [{"kind": "answer", "config": label(cfg), **r} for r in rows]

    corpus_files = len(next(iter(indexes.values()))["files"])
    summary = {
        "run_id": run_id, "created_at": started.isoformat(timespec="seconds"),
        "corpus": {"name": "TCG snapshot", "files": corpus_files, "questions": len(questions),
                   "answerable": len(answerable)},
        "indexes": index_info,
        "retrieval": {"k_values": list(K_VALUES), "configs": retrieval},
        "generation": generation_runs[0] if generation_runs else None,
        "generation_runs": generation_runs,
        "settings": {"candidates": 30, "rerank_candidates": 20, "rrf_k": 60, "retrieve_k": RETRIEVE_K,
                     "answer_top_k": 6, **PINNED_INDEXING},
        "notes": ["Retrieval metrics use the 45 answerable questions; answers use all 50.",
                  "Gold spans come from Python's ast module, independent of the chunker being measured.",
                  "Latency is wall-clock on the machine that ran the eval (see README for its specs)."],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (out_dir / "per_question.jsonl").open("w", encoding="utf-8") as f:
        for row in per_question:
            f.write(json.dumps(row) + "\n")
    report = report_md(summary)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"Results: {out_dir}")
    _exit_watchdog()


def _exit_watchdog() -> None:
    """Diagnostics: name any non-daemon threads that would keep Python from exiting,
    and dump every thread's stack if exiting takes more than 20 s."""
    left = [t.name for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon]
    if left:
        log.info("Non-daemon threads still running at exit: %s", left)
    faulthandler.dump_traceback_later(20, exit=False, file=sys.stderr)


if __name__ == "__main__":
    main()
