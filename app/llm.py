"""The LLM layer: one interface, any provider (via LiteLLM).

Everything downstream calls `complete()` / `stream()` with plain chat
messages. Which provider and model actually answer is a setting
(LLM_PROVIDER / LLM_MODEL), resolved at call time, so switching between
DeepSeek, Groq, OpenAI and Gemini needs no code changes and no restart.

LiteLLM turns "provider/model" into the right API call. The API key is passed
per call from Settings (never read from LiteLLM's own env-var conventions), so
the Settings panel is the single source of truth.

`COMPLETION` is the one function that talks to the network; tests replace it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass

from . import config

# Use LiteLLM's bundled model list instead of fetching it from the internet at import.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # noqa: E402

litellm.suppress_debug_info = True
litellm.drop_params = True          # ignore params a provider doesn't support (e.g. temperature quirks)

COMPLETION = litellm.completion     # swapped out in tests
TIMEOUT_S = 60


def _stop_litellm_logging() -> None:
    """LiteLLM logs each call on a background thread pool whose (non-daemon) threads
    Python waits for at exit, so after many calls a finished script could hang
    for minutes. We don't use LiteLLM's logging: drop the queued work instead."""
    try:
        from litellm.litellm_core_utils.thread_pool_executor import executor
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


# threading's own exit hooks run before ordinary atexit handlers (and before
# thread pools are joined), so this is the hook that's early enough.
try:
    import threading
    threading._register_atexit(_stop_litellm_logging)
except Exception:                     # private API: fall back to stopping it explicitly
    pass


@dataclass
class Target:
    provider: str
    model: str
    api_key: str

    @property
    def litellm_model(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def label(self) -> str:
        return f"{config.PROVIDER_LABELS[self.provider]} · {self.model}"


def target(provider: str | None = None, model: str | None = None) -> Target:
    """The configured provider/model/key (raises ConfigError if the key is missing)."""
    provider = provider or config.get("LLM_PROVIDER")
    if provider not in config.PROVIDERS:
        raise config.ConfigError(f"Unknown provider {provider!r}")
    model = model or (config.llm_model() if provider == config.get("LLM_PROVIDER") else config.DEFAULT_MODELS[provider])
    return Target(provider, model, config.provider_key(provider))


def _kwargs(t: Target, messages: list[dict], max_tokens: int | None, temperature: float | None) -> dict:
    return {"model": t.litellm_model, "messages": messages, "api_key": t.api_key, "timeout": TIMEOUT_S,
            "max_tokens": max_tokens or config.get("LLM_MAX_TOKENS"),
            "temperature": config.get("LLM_TEMPERATURE") if temperature is None else temperature,
            "num_retries": 2}


def complete(messages: list[dict], max_tokens: int | None = None, temperature: float | None = None,
             t: Target | None = None) -> dict:
    """One full reply: {"text", "model", "usage": {"in", "out"}, "ms"}."""
    t = t or target()
    started = time.perf_counter()
    resp = COMPLETION(**_kwargs(t, messages, max_tokens, temperature))
    usage = getattr(resp, "usage", None)
    return {"text": resp.choices[0].message.content or "", "model": t.label,
            "usage": {"in": getattr(usage, "prompt_tokens", 0) or 0, "out": getattr(usage, "completion_tokens", 0) or 0},
            "ms": round((time.perf_counter() - started) * 1000)}


def stream(messages: list[dict], max_tokens: int | None = None, temperature: float | None = None,
           t: Target | None = None) -> Iterator[str]:
    """The reply as it's generated, piece by piece."""
    t = t or target()
    for chunk in COMPLETION(**_kwargs(t, messages, max_tokens, temperature), stream=True):
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


def check_key(provider: str, api_key: str | None = None) -> tuple[bool, str]:
    """For Settings' Test button: one tiny call. Never echoes the key."""
    try:
        key = api_key or config.provider_key(provider)
    except config.ConfigError as exc:
        return False, str(exc)
    t = Target(provider, config.DEFAULT_MODELS[provider] if provider != config.get("LLM_PROVIDER")
               else config.llm_model(), key)
    try:
        COMPLETION(model=t.litellm_model, messages=[{"role": "user", "content": "Reply with: ok"}],
                   api_key=key, max_tokens=3, timeout=20)
        return True, f"Works: {t.label} answered."
    except Exception as exc:
        name = type(exc).__name__
        text = str(exc)
        if "auth" in name.lower() or "401" in text or "invalid api key" in text.lower():
            return False, f"{config.PROVIDER_LABELS[provider]} rejected the key."
        return False, f"Couldn't reach {config.PROVIDER_LABELS[provider]}: {name}."
