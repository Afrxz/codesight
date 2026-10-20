"""Phase 7.1 — trace schema + passive instrumentation.

Two Pydantic models describe what happened while the agent decided one card:

    StepTrace  one step: a tool call (fetching market data, or a look-up the
               LLM asked for) or an LLM call, with tokens, latency, cost, error
    RunTrace   everything for one task: the card, its steps, the final
               Decision, wall time and totals

How it stays "instrumentation only" (the agent's behavior must not change):

- A `Tracer` is put in a context variable by `tracing(...)`. Production code
  calls `current()` at a few points and, *only if a tracer is active*, reports
  what just happened. With no tracer (every normal run), those calls are no-ops.
- The report points are where work already happens: fetching market data
  (`run.fetch`), each LLM call (`decide.make_llm` / `make_chat`), each tool the
  model calls (`tools.Toolbox.run`, plus the calls `agent_judgment` handles itself).
- Nothing is retried, re-ordered or skipped because of tracing; a failing step
  is recorded and the original exception is re-raised unchanged.

Cost: tokens are measured from the API's usage fields. Dollars are computed
only from per-million-token prices you set (Settings: LLM_PRICE_IN_PER_M /
LLM_PRICE_OUT_PER_M); otherwise cost is None — never an invented number.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_RESULT_CHARS = 3000   # keep traces readable: big tool results are truncated


class StepTrace(BaseModel):
    step_index: int
    type: Literal["tool_call", "llm_call"]
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: Any | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    cost_usd: float | None = None          # None = prices not configured (not "free")
    timestamp: str
    error: str | None = None


class RunTrace(BaseModel):
    task_id: str
    card: dict
    steps: list[StepTrace] = Field(default_factory=list)
    final_decision: dict | None = None     # the Decision JSON, or None if none was produced
    wall_ms: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _bounded(value: Any) -> Any:
    """JSON-safe, size-bounded copy of a tool result for the trace."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value))
    if len(text) <= MAX_RESULT_CHARS:
        return json.loads(text)
    return {"truncated": text[:MAX_RESULT_CHARS]}


def _prices() -> tuple[float | None, float | None]:
    from .. import config
    return config.get("LLM_PRICE_IN_PER_M"), config.get("LLM_PRICE_OUT_PER_M")


class Tracer:
    """Collects the steps of one task. Created by `tracing(...)`."""

    def __init__(self, task_id: str, card: Mapping):
        self.trace = RunTrace(task_id=task_id, card=dict(card))
        self._started = time.perf_counter()

    def _add(self, **fields) -> StepTrace:
        step = StepTrace(step_index=len(self.trace.steps), timestamp=_now(), **fields)
        self.trace.steps.append(step)
        return step

    @contextmanager
    def tool_step(self, name: str, args: Mapping | None) -> Iterator[dict]:
        """Time a tool call. Put the result in the yielded dict under "result"."""
        slot: dict = {}
        started = time.perf_counter()
        try:
            yield slot
        except Exception as exc:
            self._add(type="tool_call", tool_name=name, tool_args=_bounded(dict(args or {})),
                      latency_ms=round((time.perf_counter() - started) * 1000, 2),
                      error=f"{type(exc).__name__}: {exc}"[:500])
            raise
        self._add(type="tool_call", tool_name=name, tool_args=_bounded(dict(args or {})),
                  tool_result=_bounded(slot.get("result")),
                  latency_ms=round((time.perf_counter() - started) * 1000, 2),
                  error=slot.get("error"))

    def tool_call(self, name: str, args: Mapping | None, result: Any = None,
                  error: str | None = None, latency_ms: float = 0.0) -> None:
        """Record a tool call that was handled without timing (e.g. refused calls)."""
        self._add(type="tool_call", tool_name=name, tool_args=_bounded(dict(args or {})),
                  tool_result=_bounded(result), latency_ms=latency_ms, error=error)

    def llm_call(self, tokens_in: int, tokens_out: int, latency_ms: float,
                 error: str | None = None) -> None:
        price_in, price_out = _prices()
        cost = None
        if price_in is not None and price_out is not None:
            cost = round(tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out, 8)
        self._add(type="llm_call", tokens_in=tokens_in, tokens_out=tokens_out,
                  latency_ms=round(latency_ms, 2), cost_usd=cost, error=error)

    def finish(self, decision) -> RunTrace:
        t = self.trace
        t.final_decision = decision.model_dump() if decision is not None else None
        t.wall_ms = round((time.perf_counter() - self._started) * 1000, 2)
        t.total_tokens = sum(s.tokens_in + s.tokens_out for s in t.steps)
        costs = [s.cost_usd for s in t.steps if s.type == "llm_call"]
        t.total_cost_usd = round(sum(costs), 8) if costs and all(c is not None for c in costs) else (
            0.0 if not costs else None)
        return t


_current: ContextVar[Tracer | None] = ContextVar("tcg_tracer", default=None)


def current() -> Tracer | None:
    """The active tracer, or None. Production code checks this and does nothing if None."""
    return _current.get()


@contextmanager
def tracing(task_id: str, card: Mapping) -> Iterator[Tracer]:
    """Activate a tracer for one task (only the eval harness does this)."""
    tracer = Tracer(task_id, card)
    token = _current.set(tracer)
    try:
        yield tracer
    finally:
        _current.reset(token)


def traced_decision(task_id: str, card: Mapping, source, history, llm=None, agent=None,
                    extra_facts=None):
    """Run the *existing* fetch -> decide path for one card under a tracer.

    Returns (Decision or None, RunTrace). This is exactly what the run loop
    does per card (fetch, apply your own price, decide); it just watches.
    """
    from ..decide import decide
    from ..run import fetch, with_manual_price

    decision = None
    with tracing(task_id, card) as tracer:
        try:
            market = fetch(source, card)
            decision = decide(card, with_manual_price(card, market), history, llm,
                              agent=agent, extra_facts=extra_facts)
        except Exception as exc:   # recorded as an abandoned run, not raised
            tracer.tool_call("decide", {}, error=f"{type(exc).__name__}: {exc}"[:500])
        return decision, tracer.finish(decision)
