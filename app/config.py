"""Settings: one registry, stored in .env, editable from the UI's Settings panel.

Every setting is declared once below (key, label, group, type, default, limits).
That single list drives validation, the Settings form, `.env.example` and the
defaults. Values are read at call time, so a change applies to the next
question or indexing job without a restart.

Secrets (API keys) are write-only from the UI: the browser only ever sees
"set" plus the last 4 characters.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv, set_key, unset_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"           # index, repo registry, logs (gitignored)
# Downloaded models: fixed, so the eval and tests (which use their own DATA_DIR)
# share one copy instead of downloading again.
MODELS_DIR = PROJECT_ROOT / "data" / "models"

load_dotenv(ENV_PATH, override=False)       # real environment variables win


def models_dir() -> Path:
    return MODELS_DIR


def qdrant_dir() -> Path:
    return DATA_DIR / "qdrant"


def repos_dir() -> Path:
    """Where git URLs are cloned to."""
    return DATA_DIR / "repos"


class ConfigError(ValueError):
    """A missing or invalid setting, with a message fit to show the user."""


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    kind: str                                  # secret | choice | number | int | text | url
    default: object = None
    help: str = ""
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    # For choices: optional labels shown in the UI.
    choice_labels: dict = field(default_factory=dict)


PROVIDERS = ("deepseek", "groq", "openai", "gemini")
PROVIDER_LABELS = {"deepseek": "DeepSeek", "groq": "Groq", "openai": "OpenAI", "gemini": "Google Gemini"}
# The model used when LLM_MODEL is left empty.
DEFAULT_MODELS = {"deepseek": "deepseek-chat", "groq": "llama-3.3-70b-versatile",
                  "openai": "gpt-4o-mini", "gemini": "gemini-2.0-flash"}
PROVIDER_KEYS = {"deepseek": "DEEPSEEK_API_KEY", "groq": "GROQ_API_KEY",
                 "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}

EMBED_MODELS = ("BAAI/bge-small-en-v1.5", "jinaai/jina-embeddings-v2-base-code", "BAAI/bge-base-en-v1.5")

SETTINGS: list[Setting] = [
    # API keys
    Setting("DEEPSEEK_API_KEY", "DeepSeek API key", "API keys", "secret",
            help="From platform.deepseek.com. The default provider."),
    Setting("GROQ_API_KEY", "Groq API key", "API keys", "secret", help="From console.groq.com (optional)."),
    Setting("OPENAI_API_KEY", "OpenAI API key", "API keys", "secret", help="From platform.openai.com (optional)."),
    Setting("GEMINI_API_KEY", "Gemini API key", "API keys", "secret", help="From aistudio.google.com (optional)."),
    # Answering
    Setting("LLM_PROVIDER", "Provider", "Answering", "choice", "deepseek", choices=PROVIDERS,
            choice_labels=PROVIDER_LABELS,
            help="Which LLM writes the answers. Switching needs only that provider's key."),
    Setting("LLM_MODEL", "Model (optional)", "Answering", "text", "",
            help="Leave empty for the provider's default: " +
                 ", ".join(f"{PROVIDER_LABELS[p]} {m}" for p, m in DEFAULT_MODELS.items()) + "."),
    Setting("LLM_TEMPERATURE", "Temperature", "Answering", "number", 0.1, minimum=0, maximum=1,
            help="Lower = more literal answers. 0.1 suits code questions."),
    Setting("LLM_MAX_TOKENS", "Longest answer", "Answering", "int", 1200, minimum=100, maximum=8000,
            unit="tokens"),
    # Retrieval
    Setting("RETRIEVAL_MODE", "Search", "Retrieval", "choice", "hybrid", choices=("hybrid", "dense", "sparse"),
            choice_labels={"hybrid": "Hybrid: meaning + keywords (best)", "dense": "Meaning only (embeddings)",
                           "sparse": "Keywords only (BM25)"}),
    Setting("RERANK", "Re-rank with the cross-encoder", "Retrieval", "choice", "on", choices=("on", "off"),
            help="More accurate ordering; adds a fraction of a second per question on a laptop CPU."),
    Setting("TOP_K", "Code chunks given to the LLM", "Retrieval", "int", 6, minimum=1, maximum=20),
    Setting("CANDIDATES", "Candidates per search", "Retrieval", "int", 30, minimum=5, maximum=100,
            help="How many results each search (meaning, keywords) returns before fusion."),
    Setting("RERANK_CANDIDATES", "Candidates re-ranked", "Retrieval", "int", 20, minimum=5, maximum=100,
            help="Fewer = faster re-ranking on slow machines."),
    Setting("RRF_K", "RRF constant k", "Retrieval", "int", 60, minimum=1, maximum=200,
            help="Reciprocal Rank Fusion: score = sum of 1/(k + rank). 60 is the standard value."),
    # Indexing
    Setting("CHUNKER", "Chunking", "Indexing", "choice", "ast", choices=("ast", "window"),
            choice_labels={"ast": "By function/class (tree-sitter)", "window": "Fixed windows of lines"},
            help="Applies to repos indexed (or re-indexed) after the change."),
    Setting("WINDOW_LINES", "Window size", "Indexing", "int", 60, minimum=10, maximum=400, unit="lines",
            help="Fixed-window chunking, and the fallback for files tree-sitter can't parse."),
    Setting("WINDOW_OVERLAP", "Window overlap", "Indexing", "int", 15, minimum=0, maximum=200, unit="lines"),
    Setting("EMBED_MODEL", "Embedding model", "Indexing", "choice", EMBED_MODELS[0], choices=EMBED_MODELS,
            choice_labels={EMBED_MODELS[0]: "bge-small (fast, ~130 MB)",
                           EMBED_MODELS[1]: "jina code (code-specific, ~640 MB, slower)",
                           EMBED_MODELS[2]: "bge-base (~440 MB)"},
            help="Runs locally on the CPU. Downloaded once. Changing it needs a re-index."),
    Setting("EMBED_BATCH", "Embedding batch size", "Indexing", "int", 8, minimum=1, maximum=256,
            help="8 is the measured sweet spot: half the memory of 32 and no slower. "
                 "Lower it further if a small laptop runs out of memory while indexing."),
    Setting("MAX_FILE_KB", "Skip files larger than", "Indexing", "int", 300, minimum=10, maximum=5000, unit="KB"),
    Setting("EXTRA_IGNORE", "Also ignore (globs)", "Indexing", "text", "",
            help="Comma-separated, on top of .gitignore, e.g. docs/**, *.min.js"),
    # Storage
    Setting("QDRANT_URL", "Qdrant server URL (optional)", "Storage", "url", "",
            help="Empty = embedded Qdrant in the data/ folder (no server needed)."),
    Setting("QDRANT_API_KEY", "Qdrant API key (optional)", "Storage", "secret"),
]

BY_KEY = {s.key: s for s in SETTINGS}

_SAFE_TEXT = re.compile(r"^[^\r\n\x00]*$")       # no line breaks: nothing can inject extra .env lines
_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


def _convert(s: Setting, raw: str):
    """Text from .env/the form -> typed value, or ConfigError."""
    raw = raw.strip()
    if s.kind in ("secret", "text"):
        if not _SAFE_TEXT.match(raw) or len(raw) > 500:
            raise ConfigError(f"{s.label}: invalid characters")
        return raw
    if s.kind == "url":
        if raw and not _URL.match(raw):
            raise ConfigError(f"{s.label}: must be an http(s) URL")
        return raw.rstrip("/")
    if s.kind == "choice":
        if raw not in s.choices:
            raise ConfigError(f"{s.label}: must be one of {', '.join(s.choices)}")
        return raw
    if s.kind in ("number", "int"):
        try:
            value = float(raw)
        except ValueError:
            raise ConfigError(f"{s.label}: must be a number") from None
        if s.kind == "int":
            if value != int(value):
                raise ConfigError(f"{s.label}: must be a whole number")
            value = int(value)
        if (s.minimum is not None and value < s.minimum) or (s.maximum is not None and value > s.maximum):
            raise ConfigError(f"{s.label}: must be between {s.minimum:g} and {s.maximum:g}")
        return value
    raise ConfigError(f"unknown setting kind {s.kind}")


def get(key: str):
    """The current typed value (environment / .env), else the default."""
    s = BY_KEY[key]
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return s.default if s.kind != "secret" else None
    try:
        return _convert(s, raw)
    except ConfigError:
        return s.default            # a hand-edited bad value never crashes the app


def llm_model() -> str:
    """The model to use: LLM_MODEL, or the provider's default."""
    return get("LLM_MODEL") or DEFAULT_MODELS[get("LLM_PROVIDER")]


def provider_key(provider: str | None = None) -> str:
    """The API key for a provider, or a ConfigError that says where to add it."""
    provider = provider or get("LLM_PROVIDER")
    key = get(PROVIDER_KEYS[provider])
    if not key:
        raise ConfigError(f"No {PROVIDER_LABELS[provider]} API key yet. Add it in Settings → API keys.")
    return key


def save(updates: dict, remove: list[str] | tuple = ()) -> None:
    """Validate everything first, then write .env (so a bad value changes nothing)."""
    checked = {}
    for key, value in updates.items():
        if key not in BY_KEY:
            raise ConfigError(f"unknown setting {key}")
        s = BY_KEY[key]
        text = "" if value is None else str(value)
        if s.kind == "secret" and text.strip() == "":
            continue                     # empty secret field = keep the saved one
        checked[key] = _convert(s, text) if text.strip() != "" else ""
    for key in remove:
        if key not in BY_KEY:
            raise ConfigError(f"unknown setting {key}")
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.touch(exist_ok=True)
    for key, value in checked.items():
        if value == "":
            unset_key(str(ENV_PATH), key)
            os.environ.pop(key, None)
        else:
            set_key(str(ENV_PATH), key, str(value), quote_mode="never")
            os.environ[key] = str(value)
    for key in remove:
        unset_key(str(ENV_PATH), key)
        os.environ.pop(key, None)


def public_view() -> list[dict]:
    """Every setting for the UI. Secrets show only whether they're set + a hint."""
    out = []
    for s in SETTINGS:
        item = {"key": s.key, "label": s.label, "group": s.group, "kind": s.kind, "help": s.help,
                "default": s.default, "choices": list(s.choices), "choice_labels": s.choice_labels,
                "minimum": s.minimum, "maximum": s.maximum, "unit": s.unit}
        value = get(s.key)
        if s.kind == "secret":
            item.update(is_set=bool(value), hint=f"…{value[-4:]}" if value and len(value) > 8 else "")
        else:
            item["value"] = value
        out.append(item)
    return out
