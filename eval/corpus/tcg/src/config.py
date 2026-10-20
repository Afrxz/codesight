"""Centralised configuration.

Every tunable value lives in one registry, `SETTINGS`. Each entry declares its
env-var name, type, default and limits, and that single declaration drives:

- reading:    `get("BUY_EDGE_PCT")` returns a parsed, typed value (or the default),
- validating: `validate(key, raw)` rejects bad input with a readable message,
- saving:     `save({...})` writes `.env` and updates the running process,
- the UI:     `describe()` is what the binder's Settings panel renders from.

Values come from the environment, normally via the project's `.env` file.
Real environment variables win over `.env` (useful for CI / servers).

Design choice: required keys fail *loudly and early* with a human-readable
message, rather than as a cryptic auth error deep inside an API call later.
Secret values (API keys) are never returned by `describe()`; it only reports
whether a key is set and its last four characters.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values, load_dotenv, set_key, unset_key

# Project paths, derived from this file's location so they work regardless of
# the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "app.db"
ENV_PATH = PROJECT_ROOT / ".env"

# Load .env once, at import time. `override=False`: real env vars take precedence.
load_dotenv(ENV_PATH, override=False)


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing or invalid.

    `errors` maps setting keys to messages when several fields failed at once.
    """

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(message)
        self.errors = errors or {}


# --- The settings registry ---------------------------------------------------- #

@dataclass(frozen=True)
class Setting:
    key: str                     # env-var name, e.g. "LLM_MODEL"
    label: str
    group: str
    kind: str                    # "secret" | "secret_url" | "url" | "text" | "choice" | "number" | "time"
    default: str | float | None = None
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""               # shown next to number inputs: "%", "h", "s"


SETTINGS: list[Setting] = [
    # API keys
    Setting("LLM_API_KEY", "LLM API key", "API keys", "secret",
            help="Key for your LLM provider (DeepSeek by default). Without it, "
                 "decisions use the built-in rules instead of the LLM."),
    Setting("POKEMONTCG_API_KEY", "pokemontcg.io API key (optional)", "API keys", "secret",
            help="Free from dev.pokemontcg.io. Not required: it only raises the rate limit "
                 "for card art and the free TCGplayer prices."),
    Setting("JUSTTCG_API_KEY", "JustTCG API key", "API keys", "secret",
            help="Free at justtcg.com (100 requests a day, personal use). Prices by "
                 "condition and printing, including recent sets."),
    Setting("TCGAPI_KEY", "TCG API key", "API keys", "secret",
            help="From tcgapi.net. Only needed for the tcgapi.net market data source. "
                 "Sold comps for graded cards need their Pro plan."),
    # AI model
    Setting("LLM_BASE_URL", "API base URL", "AI model", "url", "https://api.deepseek.com",
            help="Any OpenAI-compatible endpoint."),
    Setting("LLM_MODEL", "Model", "AI model", "text", "deepseek-chat",
            help="The model name your provider expects."),
    Setting("LLM_PRICE_IN_PER_M", "Price per 1M input tokens (optional)", "AI model", "number", None,
            minimum=0, maximum=1000, unit="USD",
            help="From your provider's pricing page. Used only to report eval costs; "
                 "left empty, costs show as 'not configured'."),
    Setting("LLM_PRICE_OUT_PER_M", "Price per 1M output tokens (optional)", "AI model", "number", None,
            minimum=0, maximum=1000, unit="USD"),
    # Agent runs
    Setting("RUN_SOURCE", "Market data", "Agent runs", "choice", "mock",
            choices=("mock", "pokemontcg", "justtcg", "tcgapi", "mcp"),
            help="Where 'Re-run agent' gets prices. History is kept separately per source, "
                 "so switching never mixes sample and real prices. 'mcp' gets them through "
                 "the project's MCP server (same data as the source it wraps)."),
    Setting("MCP_SOURCE", "MCP server wraps", "Agent runs", "choice", "justtcg",
            choices=("mock", "pokemontcg", "justtcg", "tcgapi"),
            help="The price source behind the MCP server (used when Market data is 'mcp', "
                 "and by other MCP clients)."),
    Setting("RUN_LLM", "Use the LLM", "Agent runs", "choice", "auto",
            choices=("auto", "on", "off"),
            help="Whether recommendations are written by the LLM or by the built-in rules."),
    # Decision rules (also written into the LLM prompt, so both stay in step)
    Setting("BUY_EDGE_PCT", "Buy when cheapest listing is under market by", "Decision rules",
            "number", 15.0, minimum=1, maximum=90, unit="%",
            help="A watched card whose cheapest listing is at least this far below "
                 "its sold average is a buy."),
    Setting("STRONG_EDGE_PCT", "…with high confidence when under by", "Decision rules",
            "number", 20.0, minimum=1, maximum=95, unit="%",
            help="Must be at least the buy threshold."),
    Setting("BIG_GAIN_PCT", "Consider selling once you're up", "Decision rules",
            "number", 50.0, minimum=1, maximum=1000, unit="%",
            help="An owned card this far above its cost basis is a sell if the trend turns down."),
    Setting("TREND_THRESHOLD_PCT", "Count a price move as up/down from", "Decision rules",
            "number", 3.0, minimum=0.5, maximum=50, unit="%",
            help="Smaller moves in the sold average over 30 days count as flat."),
    # Agent behaviour
    Setting("AUTO_RUN", "Run automatically", "Agent", "choice", "on", choices=("on", "off"),
            help="While the binder is open, the agent runs once a day at the time below "
                 "(or as soon as you open it, if today's run was missed)."),
    Setting("RUN_TIME", "Daily run time", "Agent", "time", "09:00",
            help="Your computer's local time, 24-hour clock."),
    Setting("AGENT_TOOLS", "Let the LLM look things up", "Agent", "choice", "on", choices=("on", "off"),
            help="When signals conflict, the LLM can check longer price history, other "
                 "conditions/printings, a second price source or the set's movement before deciding."),
    Setting("AGENT_TOOL_BUDGET", "Look-ups per card, at most", "Agent", "number", 3.0,
            minimum=0, maximum=5,
            help="Caps cost and time. 0 = decide from the basic facts only."),
    Setting("ALERT_MOVE_PCT", "Alert when a price moves by", "Agent", "number", 15.0,
            minimum=1, maximum=90, unit="%",
            help="Over the last 7 days. Also used for 'well below its 30-day average' alerts."),
    Setting("ALERT_WEBHOOK_URL", "Send alerts to (webhook, optional)", "Agent", "secret_url",
            help="A Discord or Slack incoming-webhook URL. Leave empty to only show alerts in the binder."),
    Setting("BUY_BUDGET", "Monthly buying budget (optional)", "Agent", "number", 0.0,
            minimum=0, maximum=1_000_000, unit="USD",
            help="The collection review suggests how to spend it across your watchlist. 0 = no budget."),
    Setting("SEARXNG_URL", "SearXNG URL for web search (optional)", "Agent", "url", None,
            help="Your own SearXNG instance with JSON output enabled, e.g. http://localhost:8080. "
                 "Gives the LLM a web_search tool. Results are treated as untrusted text."),
    # Live data
    Setting("CACHE_TTL_HOURS", "Reuse live API responses for", "Live data",
            "number", 24.0, minimum=0, maximum=168, unit="h",
            help="Saves your daily request quota. 0 always fetches fresh data."),
    Setting("REQUEST_INTERVAL_S", "Wait between live requests", "Live data",
            "number", 1.0, minimum=0.2, maximum=60, unit="s",
            help="Spacing between calls to tcgapi.net, to stay inside rate limits."),
]
REGISTRY = {s.key: s for s in SETTINGS}
# Old spellings still accepted: "real" was the plan's name for tcgapi.net.
CHOICE_ALIASES = {"RUN_SOURCE": {"real": "tcgapi"}}

# Conservative character sets. Because values can only contain these, nothing
# typed into the Settings panel can inject extra lines or comments into .env.
_SECRET_RE = re.compile(r"^[A-Za-z0-9._\-:+=/]{8,300}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\-]{1,100}$")


def get(key: str):
    """Current value of a setting, parsed to its type; the default if unset or invalid."""
    setting = REGISTRY[key]
    raw = os.getenv(key, "").strip()
    if not raw:
        return setting.default
    try:
        return float(validate(key, raw)) if setting.kind == "number" else validate(key, raw)
    except ConfigError:
        return setting.default   # a hand-edited bad value shouldn't crash a run


def validate(key: str, raw: str) -> str:
    """Return the normalized string to store for `key`, or raise ConfigError."""
    setting = REGISTRY.get(key)
    if setting is None:
        raise ConfigError(f"Unknown setting {key!r}.")
    value = str(raw).strip()

    if setting.kind == "secret":
        if not _SECRET_RE.match(value):
            raise ConfigError("That doesn't look like an API key (8–300 characters, "
                              "letters, digits and . _ - : + = / only).")
    elif setting.kind == "secret_url":
        # A URL that contains a token (webhooks). https only, and the same
        # restricted character set as keys, so it can't inject into .env.
        if not value.startswith("https://") or not _SECRET_RE.match(value):
            raise ConfigError("Use an https:// webhook URL (letters, digits and . _ - : + = / only).")
    elif setting.kind == "time":
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
            raise ConfigError("Use a 24-hour time like 09:00 or 21:30.")
    elif setting.kind == "url":
        parsed = urlparse(value)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        if not parsed.hostname or parsed.scheme not in ("https", "http") \
                or (parsed.scheme == "http" and not local) or any(c.isspace() for c in value):
            raise ConfigError("Use an https:// URL (http:// is only allowed for localhost).")
        value = value.rstrip("/")
    elif setting.kind == "text":
        if not _MODEL_RE.match(value):
            raise ConfigError("Use letters, digits and . _ : / - only (no spaces).")
    elif setting.kind == "choice":
        value = CHOICE_ALIASES.get(key, {}).get(value, value)
        if value not in setting.choices:
            raise ConfigError(f"Choose one of: {', '.join(setting.choices)}.")
    elif setting.kind == "number":
        try:
            number = float(value)
        except ValueError:
            raise ConfigError("Enter a number.") from None
        if not (setting.minimum <= number <= setting.maximum):
            raise ConfigError(f"Enter a value from {setting.minimum:g} to {setting.maximum:g}.")
        value = f"{number:g}"
    return value


def save(updates: dict[str, str | None]) -> None:
    """Validate and persist settings to .env, and apply them to this process.

    `None` (or "") removes a setting so it falls back to its default. All
    values are validated first: if any is invalid, nothing is written, and a
    ConfigError carries a {key: message} dict in `.errors`.
    """
    errors: dict[str, str] = {}
    clean: dict[str, str | None] = {}
    for key, raw in updates.items():
        if raw is None or str(raw).strip() == "":
            if key not in REGISTRY:
                errors[key] = f"Unknown setting {key!r}."
            else:
                clean[key] = None
            continue
        try:
            clean[key] = validate(key, raw)
        except ConfigError as exc:
            errors[key] = str(exc)

    # Cross-field rule: the high-confidence edge can't be smaller than the buy edge.
    buy = float(clean.get("BUY_EDGE_PCT") or get("BUY_EDGE_PCT")) if "BUY_EDGE_PCT" not in errors else None
    strong = float(clean.get("STRONG_EDGE_PCT") or get("STRONG_EDGE_PCT")) if "STRONG_EDGE_PCT" not in errors else None
    if buy is not None and strong is not None and strong < buy:
        errors["STRONG_EDGE_PCT"] = "Must be at least the buy threshold."

    if errors:
        raise ConfigError("Some settings are invalid.", errors)

    ENV_PATH.touch(exist_ok=True)   # python-dotenv won't create the file itself
    in_file = dotenv_values(ENV_PATH)
    for key, value in clean.items():
        if value is None:
            os.environ.pop(key, None)
            if key in in_file:
                unset_key(ENV_PATH, key, quote_mode="never")
        else:
            set_key(ENV_PATH, key, value, quote_mode="never")
            os.environ[key] = value


def _mask(value: str) -> str:
    return f"ends in …{value[-4:]}" if len(value) >= 12 else "saved"


def describe() -> list[dict]:
    """The settings as the UI sees them. Secret values are never included."""
    out = []
    for s in SETTINGS:
        item = {
            "key": s.key, "label": s.label, "group": s.group, "kind": s.kind,
            "help": s.help, "default": s.default, "choices": list(s.choices),
            "min": s.minimum, "max": s.maximum, "unit": s.unit,
        }
        if s.kind in ("secret", "secret_url"):
            raw = os.getenv(s.key, "").strip()
            item |= {"is_set": bool(raw), "hint": _mask(raw) if raw else None}
        else:
            item["value"] = get(s.key)
        out.append(item)
    return out


# --- Typed accessors used by the rest of the app --------------------------- #

def _require(name: str) -> str:
    """Return the env var `name`, or raise a clear ConfigError if it's absent/blank."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Add it in the binder's Settings, "
            f"or set {name}=<value> in the .env file."
        )
    return value


@dataclass(frozen=True)
class Settings:
    """Typed, immutable view of the LLM connection settings."""

    llm_api_key: str
    llm_base_url: str
    llm_model: str


def get_settings() -> Settings:
    """LLM connection settings. Raises ConfigError if LLM_API_KEY is missing."""
    return Settings(
        llm_api_key=_require("LLM_API_KEY"),
        llm_base_url=get("LLM_BASE_URL"),
        llm_model=get("LLM_MODEL"),
    )


def get_tcgapi_key() -> str:
    """Live market API key. Separate from get_settings so mock runs never need it."""
    return _require("TCGAPI_KEY")


def get_justtcg_key() -> str:
    """JustTCG API key; only needed when JustTCG is the market data source."""
    return _require("JUSTTCG_API_KEY")
