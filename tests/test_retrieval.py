"""BM25 tokenizer/weights, RRF math, indexing (incl. incremental) and the retrieval pipeline."""

import pytest

from app import bm25, config, indexer, store
from app.retrieval import retrieve, rrf


def test_tokenizer_splits_identifiers_and_folds_plurals():
    toks = bm25.tokenize("def fetch_with_retry(getMarketData): retries = 3  # HTTPServer 429")
    assert {"fetch_with_retry", "fetch", "retry", "getmarketdata", "get", "market", "data"} <= set(toks)
    assert "retry" in toks and "429" in toks and "http" in toks and "server" in toks
    assert "with" not in toks                                  # stopword (the whole identifier stays)
    assert bm25.tokenize("Where are RETRIES handled?") == ["retry", "handled"]


def test_bm25_document_weights():
    idx, val = bm25.doc_vector(["retry", "retry", "fetch"], avgdl=3)
    weights = dict(zip(idx, val))
    w_retry, w_fetch = weights[bm25.token_id("retry")], weights[bm25.token_id("fetch")]
    # tf saturation: twice the term frequency is worth more, but less than twice as much.
    assert w_fetch < w_retry < 2 * w_fetch
    # A longer document (same tf) gets a lower weight.
    long_idx, long_val = bm25.doc_vector(["retry"] + ["x"] * 30, avgdl=3)
    assert dict(zip(long_idx, long_val))[bm25.token_id("retry")] < w_fetch


def test_rrf_by_hand():
    scores = rrf([["a", "b", "c"], ["c", "a"]], k=60)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)
    assert sorted(scores, key=lambda x: -scores[x]) == ["a", "c", "b"]   # in both lists beats one list


@pytest.fixture
def indexed(sample_repo):
    repo = indexer.new_repo(str(sample_repo), "sample")
    indexer.reindex(repo["id"])
    return store.get_repo(repo["id"])


def test_indexing_builds_a_collection(indexed):
    assert indexed["status"] == "ready" and indexed["stats"]["files"] == 4
    assert indexed["stats"]["chunks"] == store.client().count(indexed["collection"]).count
    assert indexed["avgdl"] > 0 and set(indexed["files"]) >= {"pkg/net.py", "web/app.js"}


def test_exact_identifier_is_found_by_keyword_search(indexed):
    r = retrieve(indexed, "clear_everything", mode="sparse", rerank=False, top_k=3)
    assert "Cache" in r.hits[0].symbol.split(", ") and r.hits[0].sparse_rank == 1 and r.hits[0].dense_rank is None


def test_hybrid_fuses_and_reports_every_stage(indexed):
    r = retrieve(indexed, "retry download with exponential backoff", mode="hybrid", rerank=True, top_k=3)
    top = r.hits[0]
    assert "fetch_with_retry" in top.symbol.split(", ")
    assert top.dense_rank and top.sparse_rank and top.rrf_score and top.rerank_score is not None
    assert {"dense_ms", "sparse_ms", "fuse_ms", "rerank_ms", "total_ms"} <= set(r.timings_ms)
    assert [h.rank for h in r.hits] == [1, 2, 3]


def test_incremental_reindex_touches_only_changed_files(indexed, sample_repo):
    (sample_repo / "web" / "app.js").write_text("export function checkout(cart) { return cart; }\n", encoding="utf-8")
    (sample_repo / "README.md").unlink()
    result = indexer.reindex(indexed["id"])
    assert result["changed_files"] == 1 and result["removed_files"] == 1 and not result["rebuilt"]
    repo = store.get_repo(indexed["id"])
    paths = {p.payload["path"] for p in store.client().scroll(repo["collection"], limit=500)[0]}
    assert "README.md" not in paths
    assert retrieve(repo, "checkout", mode="sparse", rerank=False, top_k=1).hits[0].symbol == "checkout"
    assert retrieve(repo, "renderInvoice", mode="sparse", rerank=False, top_k=5).hits == [] or \
        all(h.symbol != "renderInvoice" for h in retrieve(repo, "renderInvoice", mode="sparse", rerank=False).hits)


def test_changing_the_chunker_rebuilds(indexed):
    config.save({"CHUNKER": "window"})
    result = indexer.reindex(indexed["id"])
    repo = store.get_repo(indexed["id"])
    assert result["rebuilt"] and repo["chunker"] == "window" and repo["collection"] != indexed["collection"]
    assert not store.client().collection_exists(indexed["collection"])           # the old index is dropped


def test_new_chunking_rules_rebuild_old_indexes(indexed, monkeypatch):
    from app import indexer as idx
    monkeypatch.setattr(idx, "CHUNKER_VERSION", 999)
    assert indexer.reindex(indexed["id"])["rebuilt"]
    assert store.get_repo(indexed["id"])["chunker_version"] == 999


def test_duplicate_source_is_refused(sample_repo, indexed):
    with pytest.raises(ValueError, match="Already added"):
        indexer.new_repo(str(sample_repo))


def test_remove_drops_the_index(indexed):
    assert indexer.remove(indexed["id"]) and store.get_repo(indexed["id"]) is None
    assert not store.client().collection_exists(indexed["collection"])
