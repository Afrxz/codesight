"""Split source files into retrievable chunks.

Two strategies, so they can be compared in the eval:

1. AST-aware (tree-sitter), the default. A chunk is one whole definition:
   a function, a method, a class (when small enough), an interface/struct/...
   Never half of one. Rules:
   - A definition that fits (<= MAX_LINES lines, <= MAX_CHARS characters) is
     one chunk, decorators and docstring included.
   - A container (class, impl, module block) that's too big becomes a header
     chunk (its signature, docstring and fields: everything except its member
     definitions) plus one chunk per member, recursively.
   - A single function that's too big is cut into line windows, each one
     carrying the function's signature as context.
   - Top-level code between definitions (imports, constants, script code) is
     grouped into "module" chunks.
   - Comments directly above a definition belong to it (like its decorators),
     so a section comment never becomes a chunk of its own.
   - Optional ("ast-merge", eval only): merge adjacent small siblings in the
     same scope into chunks of up to MERGE_LINES lines. Measured on the eval
     suite it lowered both recall and precision, so it's off by default.
   Every non-blank line of the file ends up in a chunk.

2. Fixed windows of N lines with overlap: the baseline, and the fallback for
   files tree-sitter can't parse (Markdown, YAML, ...).

Each chunk keeps exact 1-based line numbers (for citations) and a `context`
line (e.g. "class Toolbox:") that is embedded with the code but isn't part of
its text, so a method still "knows" which class it belongs to.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .loader import SourceFile

# Bump when chunking rules change: indexes built by an older version are rebuilt.
CHUNKER_VERSION = 3   # 2: comments attach to definitions; 3: sibling merging off by default (measured worse)
MAX_LINES = 80        # bigger definitions are split (embedding models read ~512 tokens)
MAX_CHARS = 3500
MERGE_LINES = 30      # adjacent small siblings are merged up to this size...
MERGE_CHARS = 1800    # ...and this many characters
MERGEABLE = {"function", "method", "module", "class", "group"}


@dataclass
class Chunk:
    path: str
    language: str | None
    start_line: int            # 1-based, inclusive
    end_line: int              # 1-based, inclusive
    kind: str                  # function | method | class | module | window | part | ...
    symbol: str                # qualified name, e.g. "Toolbox.run"; "" for module/window chunks
    text: str
    context: str = ""          # enclosing signature(s), embedded with the text
    meta: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Stable id (same chunk -> same id across re-indexes), as a UUID for Qdrant."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.path}:{self.start_line}:{self.end_line}:{self.symbol}"))

    def embedding_text(self) -> str:
        """What gets embedded: where it lives + what it is + the code."""
        head = self.path + (f" · {self.symbol}" if self.symbol else "")
        return "\n".join(x for x in (head, self.context, self.text) if x)


# --- per-language definition node types -----------------------------------------
# "defs" are chunk-worthy definitions; "containers" are defs whose members can be
# split out when the whole thing is too big.

LANG_RULES: dict[str, dict[str, set[str]]] = {
    "python": {"defs": {"function_definition", "class_definition", "decorated_definition"},
               "containers": {"class_definition"}},
    "javascript": {"defs": {"function_declaration", "generator_function_declaration", "class_declaration",
                            "method_definition", "lexical_declaration", "variable_declaration"},
                   "containers": {"class_declaration", "class_body"}},
    "typescript": {"defs": {"function_declaration", "generator_function_declaration", "class_declaration",
                            "abstract_class_declaration", "method_definition", "lexical_declaration",
                            "interface_declaration", "type_alias_declaration", "enum_declaration",
                            "module", "internal_module"},
                   "containers": {"class_declaration", "abstract_class_declaration", "class_body",
                                  "module", "internal_module"}},
    "java": {"defs": {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
                      "method_declaration", "constructor_declaration"},
             "containers": {"class_declaration", "interface_declaration", "enum_declaration"}},
    "go": {"defs": {"function_declaration", "method_declaration", "type_declaration"}, "containers": set()},
    "rust": {"defs": {"function_item", "impl_item", "struct_item", "enum_item", "trait_item", "mod_item"},
             "containers": {"impl_item", "trait_item", "mod_item"}},
    "c": {"defs": {"function_definition", "struct_specifier"}, "containers": set()},
    "cpp": {"defs": {"function_definition", "class_specifier", "struct_specifier", "namespace_definition"},
            "containers": {"class_specifier", "struct_specifier", "namespace_definition"}},
    "csharp": {"defs": {"class_declaration", "interface_declaration", "struct_declaration", "enum_declaration",
                        "method_declaration", "constructor_declaration", "namespace_declaration"},
               "containers": {"class_declaration", "interface_declaration", "struct_declaration",
                              "namespace_declaration"}},
    "ruby": {"defs": {"method", "singleton_method", "class", "module"}, "containers": {"class", "module"}},
    "php": {"defs": {"function_definition", "class_declaration", "method_declaration", "interface_declaration",
                     "trait_declaration"},
            "containers": {"class_declaration", "interface_declaration", "trait_declaration"}},
    "kotlin": {"defs": {"function_declaration", "class_declaration", "object_declaration"},
               "containers": {"class_declaration", "object_declaration"}},
    "swift": {"defs": {"function_declaration", "class_declaration", "protocol_declaration"},
              "containers": {"class_declaration", "protocol_declaration"}},
    "scala": {"defs": {"function_definition", "class_definition", "object_definition", "trait_definition"},
              "containers": {"class_definition", "object_definition", "trait_definition"}},
}
LANG_RULES["tsx"] = LANG_RULES["typescript"]

KIND = {"function_definition": "function", "function_declaration": "function", "function_item": "function",
        "generator_function_declaration": "function", "method_definition": "method",
        "method_declaration": "method", "constructor_declaration": "method", "method": "method",
        "singleton_method": "method"}


def _parser(language: str):
    from tree_sitter_language_pack import get_parser
    return get_parser(language)


def _name(node) -> str:
    """A definition's name, looking through decorators/exports/variable declarations."""
    if node.type in ("decorated_definition", "export_statement"):
        inner = _unwrap(node)
        return _name(inner) if inner is not node else ""
    if node.type in ("lexical_declaration", "variable_declaration"):
        for child in node.named_children:
            if child.type == "variable_declarator":
                name = child.child_by_field_name("name")
                return name.text.decode("utf-8", "replace") if name is not None else ""
        return ""
    name = node.child_by_field_name("name")
    if name is None:
        for child in node.named_children:          # e.g. go type_declaration -> type_spec
            if child.type in ("type_spec", "type_identifier", "identifier"):
                inner = child.child_by_field_name("name") or child
                return inner.text.decode("utf-8", "replace")
        return ""
    return name.text.decode("utf-8", "replace")


def _unwrap(node):
    """The definition inside `export ...` / `@decorator ...` wrappers (for kind/container checks)."""
    if node.type == "decorated_definition":
        return node.child_by_field_name("definition") or node
    if node.type == "export_statement":
        decl = node.child_by_field_name("declaration")
        return decl if decl is not None else node
    return node


def _is_function_value(node) -> bool:
    """JS/TS `const f = () => {...}` / `const f = function () {...}` count as definitions."""
    for child in node.named_children:
        if child.type == "variable_declarator":
            value = child.child_by_field_name("value")
            if value is not None and value.type in ("arrow_function", "function_expression", "function",
                                                     "class", "generator_function"):
                return True
    return False


def _is_def(node, rules) -> bool:
    inner = _unwrap(node)
    if node.type == "export_statement" and inner is node:
        return False
    if inner.type in ("lexical_declaration", "variable_declaration"):
        return _is_function_value(inner)
    return inner.type in rules["defs"]


def _members(node, rules):
    """Direct member definitions of a container (looking into its body block)."""
    inner = _unwrap(node)
    bodies = [c for c in inner.named_children if c.type in ("block", "class_body", "declaration_list",
                                                            "body", "statement_block", "field_declaration_list",
                                                            "enum_body", "interface_body", "body_statement")]
    scope = bodies[0] if bodies else inner
    return [c for c in scope.named_children if _is_def(c, rules)]


def _lines(src_lines: list[str], start: int, end: int) -> str:
    return "\n".join(src_lines[start - 1:end])


def _signature(src_lines: list[str], node) -> str:
    """The first line of a definition (e.g. 'class Toolbox:' / 'def run(self, name, args):')."""
    inner = _unwrap(node)
    line = src_lines[inner.start_point[0]].strip()
    return line[:200]


def _window(path, language, src_lines, start, end, size, overlap, kind="window", symbol="", context=""):
    """Line windows over [start, end] (1-based inclusive), skipping all-blank windows."""
    out, step = [], max(1, size - overlap)
    s = start
    while s <= end:
        e = min(end, s + size - 1)
        text = _lines(src_lines, s, e)
        if text.strip():
            out.append(Chunk(path, language, s, e, kind, symbol, text, context))
        if e == end:
            break
        s += step
    return out


def _start(node) -> int:
    """1-based first line of a definition, including the comments right above it."""
    start = node.start_point[0]
    prev = node.prev_named_sibling
    while prev is not None and "comment" in prev.type and start - prev.end_point[0] <= 2:
        start = prev.start_point[0]
        prev = prev.prev_named_sibling
    return start + 1


def _fits(node, start: int, src_lines) -> bool:
    end = node.end_point[0] + 1
    return end - start + 1 <= MAX_LINES and len(_lines(src_lines, start, end)) <= MAX_CHARS


def _chunk_node(f: SourceFile, src_lines, node, rules, prefix: str, context: str, out: list) -> None:
    """Emit chunks for one definition node (recursing into big containers)."""
    inner = _unwrap(node)
    name = _name(node) or "<anonymous>"
    symbol = f"{prefix}.{name}" if prefix else name
    start, end = _start(node), node.end_point[0] + 1
    is_container = inner.type in rules["containers"]
    if inner.type in ("lexical_declaration", "variable_declaration"):
        kind = "function"                         # const f = () => {...}
    else:
        kind = KIND.get(inner.type, "class" if is_container else inner.type.replace("_declaration", "")
                        .replace("_definition", "").replace("_item", ""))
    if prefix and kind == "function":
        kind = "method"

    if _fits(node, start, src_lines):
        out.append(Chunk(f.path, f.language, start, end, kind, symbol, _lines(src_lines, start, end), context))
        return

    members = _members(node, rules) if is_container else []
    if members:
        # Header: the container minus its members (signature, docstring, fields, ...).
        sig = _signature(src_lines, node)
        covered = set()
        for m in members:
            covered.update(range(_start(m), m.end_point[0] + 2))
        _gap_chunks(f, src_lines, start, end, covered, out, kind=f"{kind}-header", symbol=symbol, context=context)
        child_context = "\n".join(x for x in (context, sig) if x)
        for m in members:
            _chunk_node(f, src_lines, m, rules, symbol, child_context, out)
        return

    # One huge function (or a container with no member defs): line windows, each
    # carrying the signature so a later part still says what it belongs to.
    sig = _signature(src_lines, node)
    parts = _window(f.path, f.language, src_lines, start, end, MAX_LINES, 10, kind="part", symbol=symbol,
                    context="\n".join(x for x in (context, sig) if x))
    for i, part in enumerate(parts, 1):
        part.meta["part"] = f"{i}/{len(parts)}"
    out.extend(parts)


def _gap_chunks(f: SourceFile, src_lines, start, end, covered: set, out: list, kind="module",
                symbol="", context="") -> None:
    """Group lines in [start, end] not in `covered` into contiguous chunks (split if long)."""
    run_start = None
    for line in range(start, end + 2):
        inside = line <= end and line not in covered
        if inside and run_start is None:
            run_start = line
        elif not inside and run_start is not None:
            run_end = line - 1
            text = _lines(src_lines, run_start, run_end)
            if text.strip():
                # Trim blank edges so line numbers point at real code.
                while not src_lines[run_start - 1].strip():
                    run_start += 1
                while not src_lines[run_end - 1].strip():
                    run_end -= 1
                if run_end - run_start + 1 > MAX_LINES:
                    out.extend(_window(f.path, f.language, src_lines, run_start, run_end, MAX_LINES, 10,
                                       kind=kind, symbol=symbol, context=context))
                else:
                    out.append(Chunk(f.path, f.language, run_start, run_end, kind, symbol,
                                     _lines(src_lines, run_start, run_end), context))
            run_start = None


def chunk_ast(f: SourceFile, window_lines: int = 60, window_overlap: int = 15, merge: bool = False) -> list[Chunk]:
    """AST-aware chunks; falls back to windows for unsupported or unparsable files.
    `merge=True` adds the small-sibling merge (an eval variant)."""
    rules = LANG_RULES.get(f.language or "")
    if rules is None:
        return chunk_window(f, window_lines, window_overlap)
    try:
        tree = _parser(f.language).parse(f.text.encode("utf-8"))
    except Exception:
        return chunk_window(f, window_lines, window_overlap)
    src_lines = f.text.split("\n")
    root = tree.root_node
    out: list[Chunk] = []
    defs = [n for n in root.named_children if _is_def(n, rules)]
    covered: set[int] = set()
    for node in defs:
        covered.update(range(_start(node), node.end_point[0] + 2))
    _gap_chunks(f, src_lines, 1, len(src_lines), covered, out)
    for node in defs:
        _chunk_node(f, src_lines, node, rules, "", "", out)
    out.sort(key=lambda c: (c.start_line, c.end_line))
    return merge_small(out, src_lines) if merge else out


def merge_small(chunks: list[Chunk], src_lines: list[str]) -> list[Chunk]:
    """Merge adjacent small siblings (same file, same enclosing scope) up to MERGE_LINES."""
    merged: list[Chunk] = []
    for c in chunks:
        prev = merged[-1] if merged else None
        if (prev is not None and prev.kind in MERGEABLE and c.kind in MERGEABLE and prev.context == c.context
                and c.start_line > prev.end_line and c.end_line - prev.start_line + 1 <= MERGE_LINES):
            text = _lines(src_lines, prev.start_line, c.end_line)
            if len(text) <= MERGE_CHARS:
                names = [n for n in (prev.symbol.split(", ") if prev.symbol else []) + ([c.symbol] if c.symbol else [])]
                merged[-1] = Chunk(prev.path, prev.language, prev.start_line, c.end_line,
                                   prev.kind if prev.kind == c.kind else "group", ", ".join(dict.fromkeys(names)),
                                   text, prev.context)
                continue
        merged.append(c)
    return merged


def chunk_window(f: SourceFile, window_lines: int = 60, window_overlap: int = 15) -> list[Chunk]:
    """The fixed-window baseline: N lines at a time, overlapping."""
    src_lines = f.text.split("\n")
    return _window(f.path, f.language, src_lines, 1, len(src_lines), window_lines, min(window_overlap,
                                                                                       window_lines - 1))


def chunk_file(f: SourceFile, strategy: str = "ast", window_lines: int = 60, window_overlap: int = 15) -> list[Chunk]:
    """strategy: "ast" (the default), "window", or "ast-merge" (an eval variant)."""
    if strategy == "window":
        return chunk_window(f, window_lines, window_overlap)
    return chunk_ast(f, window_lines, window_overlap, merge=strategy == "ast-merge")
