"""Download everything RAGG needs to run offline, once:  python -m app.setup_models

- the embedding model (bge-small by default, ~130 MB; --all adds the others)
- the cross-encoder re-ranker (~90 MB)
- the tree-sitter grammars (~60 MB, cached by tree-sitter-language-pack)

Otherwise each is downloaded the first time it's used. After this, indexing
and searching work with no internet; only the LLM call needs a connection.
"""

from __future__ import annotations

import argparse
import time

from . import config, models
from .chunking import LANG_RULES


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Download RAGG's local models and grammars.")
    parser.add_argument("--all", action="store_true", help="every embedding model, not just the configured one")
    args = parser.parse_args(argv)

    names = list(config.EMBED_MODELS) if args.all else [config.get("EMBED_MODEL")]
    for name in names:
        t = time.time()
        print(f"Embedding model {name} …", end=" ", flush=True)
        models.Embedder(name).embed_query("warm up")
        print(f"ready ({time.time() - t:.0f} s)")
    t = time.time()
    print(f"Re-ranker {models.RERANK_MODEL} …", end=" ", flush=True)
    models.Reranker().score("warm up", ["def f(): pass"])
    print(f"ready ({time.time() - t:.0f} s)")
    from tree_sitter_language_pack import get_parser
    t = time.time()
    print(f"Grammars ({len(LANG_RULES)} languages) …", end=" ", flush=True)
    for language in LANG_RULES:
        get_parser(language)
    print(f"ready ({time.time() - t:.0f} s)")
    print(f"Done. Models are in {config.models_dir()}.")


if __name__ == "__main__":
    main()
