"""Loader + chunkers: what's read, where chunk boundaries fall, exact line numbers."""

import textwrap
from collections import Counter

from app.chunking import MAX_LINES, MERGE_LINES, chunk_ast, chunk_window
from app.loader import SourceFile, is_git_url, load_files, repo_slug


def src(text, path="m.py", language="python"):
    return SourceFile(path=path, language=language, text=textwrap.dedent(text), sha="x")


def chunk_for(chunks, name):
    """The chunk holding `name` (small neighbours may share a chunk: symbol "a, b")."""
    return next(c for c in chunks if name in c.symbol.split(", "))


def test_loader_respects_gitignore_and_skips_junk(sample_repo):
    paths = [f.path for f in load_files(sample_repo)]
    assert paths == [".gitignore", "README.md", "pkg/net.py", "web/app.js"]
    langs = {f.path: f.language for f in load_files(sample_repo)}
    assert langs["pkg/net.py"] == "python" and langs["web/app.js"] == "javascript" and langs["README.md"] is None


def test_extra_ignore_globs(sample_repo):
    assert "README.md" not in [f.path for f in load_files(sample_repo, extra_ignore="*.md, web/**")]


def test_functions_and_classes_are_whole_chunks(sample_repo):
    f = next(f for f in load_files(sample_repo) if f.path == "pkg/net.py")
    chunks = chunk_ast(f, merge=False)                 # the split stage on its own
    fn = chunk_for(chunks, "fetch_with_retry")
    assert "def fetch_with_retry(url" in fn.text and fn.text.rstrip().endswith('raise RuntimeError("gave up")')
    cache = chunk_for(chunks, "Cache")
    assert "class Cache:" in cache.text and "def clear_everything" in cache.text
    assert any("import time" in c.text and "MAX_ATTEMPTS = 3" in c.text for c in chunks)
    # Each definition is whole: no chunk starts or ends inside one.
    for c in chunks:
        assert not c.text.lstrip().startswith(("return", "for ", "try:", "except"))


def test_line_numbers_are_exact_and_every_line_is_covered(sample_repo):
    for f in load_files(sample_repo):
        lines = f.text.split("\n")
        chunks = chunk_ast(f)
        seen = Counter()
        for c in chunks:
            assert c.text == "\n".join(lines[c.start_line - 1:c.end_line])
            seen.update(range(c.start_line, c.end_line + 1))
        assert all(seen[i + 1] >= 1 for i, line in enumerate(lines) if line.strip()), f.path


def test_javascript_definitions(sample_repo):
    f = next(f for f in load_files(sample_repo) if f.path == "web/app.js")
    chunks = chunk_ast(f)
    names = {n for c in chunk_ast(f, merge=False) for n in c.symbol.split(", ") if n}
    assert names == {"renderInvoice", "parseDate", "ShoppingCart"}
    assert "class ShoppingCart" in chunk_for(chunks, "ShoppingCart").text


def test_big_class_is_split_into_methods_with_context():
    methods = "\n".join(f"    def method_{i}(self):\n" + "".join(f"        x{j} = {j}\n" for j in range(8)) +
                        "        return x0\n" for i in range(12))
    f = src(f'class Big:\n    """A big class."""\n    LIMIT = 5\n\n{methods}')
    chunks = chunk_ast(f)
    header = next(c for c in chunks if c.kind == "class-header")
    assert header.symbol == "Big" and "LIMIT = 5" in header.text and "def method_" not in header.text
    m3 = chunk_for(chunks, "Big.method_3")
    assert m3.kind == "method" and m3.context == "class Big:" and "def method_3(self):" in m3.text
    assert all(c.end_line - c.start_line + 1 <= MAX_LINES for c in chunks)


def test_comments_above_a_definition_belong_to_it():
    f = src("x = 1\n\n# -- section: retries --\n# keeps trying\n\ndef retry():\n    return 1\n")
    chunks = chunk_ast(f)
    chunk = chunk_for(chunks, "retry")
    assert "# -- section: retries --" in chunk.text and "# keeps trying" in chunk.text
    assert not any(c.text.strip().startswith("#") and "def " not in c.text for c in chunks)


def test_small_siblings_merge_but_big_ones_stay_alone():
    small = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(12))
    big = "def big():\n" + "".join(f"    y{j} = {j}\n" for j in range(40)) + "    return y0\n"
    chunks = chunk_ast(src(small + big), merge=True)
    assert all(c.end_line - c.start_line + 1 <= MERGE_LINES for c in chunks if "big" not in c.symbol)
    assert len([c for c in chunks if "big" not in c.symbol]) < 12            # merged
    assert chunk_for(chunks, "big").symbol == "big"                          # too big to merge


def test_huge_function_is_split_into_parts_that_keep_the_signature():
    body = "".join(f"    step_{i} = compute({i})\n" for i in range(200))
    chunks = chunk_ast(src(f"def pipeline(data):\n{body}    return data\n"))
    assert len(chunks) >= 3 and all(c.kind == "part" and c.symbol == "pipeline" for c in chunks)
    assert all(c.context == "def pipeline(data):" for c in chunks)
    assert chunks[-1].meta["part"] == f"{len(chunks)}/{len(chunks)}"


def test_decorators_belong_to_their_function():
    chunks = chunk_ast(src("@app.get('/x')\n@cached\ndef handler():\n    return 1\n"))
    assert chunks[0].symbol == "handler" and chunks[0].start_line == 1 and chunks[0].text.startswith("@app.get")


def test_unparsable_or_text_files_fall_back_to_windows():
    f = SourceFile(path="notes.md", language=None, text="\n".join(f"line {i}" for i in range(1, 101)), sha="x")
    chunks = chunk_ast(f, window_lines=40, window_overlap=10)
    assert [c.kind for c in chunks] == ["window"] * 3
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 40), (31, 70), (61, 100)]


def test_window_chunker_covers_every_line():
    f = src("\n".join(f"x{i} = {i}" for i in range(137)))
    chunks = chunk_window(f, 60, 15)
    covered = set()
    for c in chunks:
        covered.update(range(c.start_line, c.end_line + 1))
    assert covered == set(range(1, 138))


def test_chunk_ids_are_stable():
    a = chunk_ast(src("def f():\n    return 1\n"))[0]
    b = chunk_ast(src("def f():\n    return 1\n"))[0]
    assert a.id == b.id


def test_git_url_rules():
    assert is_git_url("https://github.com/psf/requests") and is_git_url("https://gitlab.com/a/b.git")
    assert not is_git_url("ext::sh -c touch /tmp/x") and not is_git_url("http://github.com/a/b")
    assert not is_git_url("--upload-pack=evil") and not is_git_url("file:///etc")
    assert repo_slug("https://github.com/psf/requests.git") == "psf-requests"
    assert repo_slug(r"C:\code\My Project") == "my-project"
