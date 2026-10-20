"""Read a repository's source files: local folder, or a git URL cloned first.

What gets indexed:
- text/code files by extension (plus Dockerfile, Makefile, ...);
- minus anything .gitignore'd (root and nested .gitignore files), vendored or
  generated folders (node_modules, .venv, dist, ...), binaries, minified bundles
  and files over the size limit.
Nothing is ever executed: files are only read as text.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pathspec

# Directories never worth indexing.
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next", ".nuxt", "target", ".idea",
             ".vscode", "coverage", ".gradle", "vendor", "site-packages", ".eggs"}

# extension -> tree-sitter language name (None = text, chunked by windows)
LANGUAGES: dict[str, str | None] = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".java": "java", ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".kt": "kotlin", ".swift": "swift", ".scala": "scala",
    # text-like: indexed, chunked by windows
    ".md": None, ".rst": None, ".txt": None, ".toml": None, ".yaml": None, ".yml": None, ".json": None,
    ".ini": None, ".cfg": None, ".sh": None, ".bash": None, ".ps1": None, ".sql": None, ".html": None,
    ".css": None, ".scss": None, ".vue": None, ".svelte": None, ".xml": None, ".env.example": None,
}
NAMED_FILES = {"Dockerfile", "Makefile", "Procfile", "requirements.txt", "Gemfile", ".gitignore"}


@dataclass
class SourceFile:
    path: str                  # relative to the repo root, forward slashes
    language: str | None       # tree-sitter language, or None for plain text
    text: str
    sha: str                   # content hash, for incremental re-indexing


def language_for(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    for ext, lang in LANGUAGES.items():
        if name.endswith(ext):
            return lang
    return None


def is_indexable(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in NAMED_FILES or any(name.endswith(ext) for ext in LANGUAGES)


def _gitignore_specs(root: Path) -> list[tuple[Path, pathspec.PathSpec]]:
    specs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if ".gitignore" in filenames:
            lines = (Path(dirpath) / ".gitignore").read_text(encoding="utf-8", errors="ignore").splitlines()
            specs.append((Path(dirpath), pathspec.PathSpec.from_lines("gitignore", lines)))
    return specs


def _ignored(rel_to_root: Path, root: Path, specs, extra: pathspec.PathSpec | None, is_dir: bool) -> bool:
    absolute = root / rel_to_root
    for base, spec in specs:
        try:
            rel = absolute.relative_to(base).as_posix()
        except ValueError:
            continue
        if spec.match_file(rel + ("/" if is_dir else "")):
            return True
    return bool(extra and extra.match_file(rel_to_root.as_posix() + ("/" if is_dir else "")))


def _looks_minified(text: str) -> bool:
    lines = text.splitlines() or [""]
    return len(text) > 5000 and len(text) / len(lines) > 300


def load_files(root: Path, max_kb: int = 300, extra_ignore: str = "") -> list[SourceFile]:
    """Every indexable text file under `root`, sorted by path."""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Not a folder: {root}")
    specs = _gitignore_specs(root)
    extra_patterns = [p.strip() for p in extra_ignore.split(",") if p.strip()]
    extra = pathspec.PathSpec.from_lines("gitignore", extra_patterns) if extra_patterns else None
    files: list[SourceFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_dir = here.relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")
                             and not _ignored(rel_dir / d, root, specs, extra, is_dir=True))
        for name in sorted(filenames):
            rel = (rel_dir / name)
            rel_posix = rel.as_posix()
            if not is_indexable(rel_posix) or _ignored(rel, root, specs, extra, is_dir=False):
                continue
            full = here / name
            try:
                if full.stat().st_size > max_kb * 1024:
                    continue
                raw = full.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8000]:            # binary
                continue
            # Normalize Windows line endings (line numbers are unchanged).
            text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            if _looks_minified(text) or not text.strip():
                continue
            files.append(SourceFile(path=rel_posix, language=language_for(rel_posix), text=text,
                                    sha=hashlib.sha256(raw).hexdigest()))
    return sorted(files, key=lambda f: f.path)


# --- git URLs -------------------------------------------------------------------

_GIT_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/-]+?(\.git)?/?$")


def is_git_url(source: str) -> bool:
    return bool(_GIT_URL.match(source.strip()))


def repo_slug(source: str) -> str:
    """A short, filesystem-safe name: 'owner-name' for URLs, the folder name for paths."""
    s = source.strip().rstrip("/")
    if is_git_url(s):
        parts = s.removesuffix(".git").split("/")[-2:]
        s = "-".join(parts)
    else:
        s = Path(s).name or "repo"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-").lower() or "repo"


def clone(url: str, dest: Path) -> Path:
    """Shallow-clone a public https git repo (only https; never a shell, never prompts)."""
    if not is_git_url(url):
        raise ValueError("Only https:// git URLs are supported, e.g. https://github.com/owner/repo")
    if dest.exists():
        subprocess.run(["git", "-C", str(dest), "pull", "--ff-only", "--depth", "1"], check=True,
                       capture_output=True, text=True, timeout=600,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "clone", "--depth", "1", "--", url, str(dest)], capture_output=True,
                            text=True, timeout=600, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()[:300] or 'unknown error'}")
    return dest
