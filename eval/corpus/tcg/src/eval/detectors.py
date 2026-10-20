"""Phase 7.4 — failure-mode detectors: scan a RunTrace for known ways agents go wrong.

    hallucinated_tool_args  the model called a tool that doesn't exist, or passed
                            arguments outside the tool's schema (unknown names,
                            values outside an enum, wrong types)
    loop                    the same tool with the same arguments, repeated more
                            than LOOP_THRESHOLD times
    silent_abandonment      the run ended without a decision, or with an empty reason
    schema_violation        the final output doesn't validate as a Decision
    excessive_budget        too many steps, tokens or dollars for one task

Detectors are independent of the graders: a run can pass every grader and
still be flagged here (e.g. it looped three times before getting it right).
That's the point — they measure *how* the agent got there, not just the answer.
"""

from __future__ import annotations

import json
from collections import Counter

from pydantic import BaseModel, ValidationError

from ..decide import Decision
from ..tools import SCHEMAS
from .trace import RunTrace

LOOP_THRESHOLD = 2             # same call allowed twice; a third identical call is a loop
MAX_STEPS = 12                 # fetch + up to 3 look-ups + LLM rounds + submit fits well under this
MAX_TOKENS = 20_000
MAX_COST_USD = 0.05

# Tools the model may call (from the real tool schemas). fetch_market_data is
# the harness's own step, not a model call, so its args aren't checked here.
_TYPE_CHECKS = {"integer": int, "string": str, "number": (int, float), "boolean": bool}


class Detection(BaseModel):
    detector: str
    fired: bool
    detail: str = ""


def _arg_problems(name: str, args: dict | None) -> list[str]:
    schema = SCHEMAS.get(name)
    if schema is None:
        return [f"unknown tool {name!r}"]
    props = schema["parameters"].get("properties", {})
    problems = []
    for key, value in (args or {}).items():
        spec = props.get(key)
        if spec is None:
            problems.append(f"{name}: unexpected argument {key!r}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{name}: {key}={value!r} not in {spec['enum']}")
        expected = _TYPE_CHECKS.get(spec.get("type"))
        if expected and not isinstance(value, expected):
            problems.append(f"{name}: {key} should be {spec['type']}, got {type(value).__name__}")
    for key in schema["parameters"].get("required", []):
        if key not in (args or {}):
            problems.append(f"{name}: missing required argument {key!r}")
    return problems


def detect_hallucinated_tool_args(trace: RunTrace) -> Detection:
    problems = []
    for step in trace.steps:
        if step.type == "tool_call" and step.tool_name not in ("fetch_market_data", "decide"):
            problems += _arg_problems(step.tool_name, step.tool_args)
    return Detection(detector="hallucinated_tool_args", fired=bool(problems), detail="; ".join(problems))


def detect_loop(trace: RunTrace, threshold: int = LOOP_THRESHOLD) -> Detection:
    calls = Counter((s.tool_name, json.dumps(s.tool_args, sort_keys=True))
                    for s in trace.steps if s.type == "tool_call")
    loops = [f"{name} x{n}" for (name, _), n in calls.items() if n > threshold]
    return Detection(detector="loop", fired=bool(loops), detail=", ".join(loops))


def detect_silent_abandonment(trace: RunTrace) -> Detection:
    d = trace.final_decision
    if d is None:
        return Detection(detector="silent_abandonment", fired=True, detail="no final decision")
    if not str(d.get("reason") or "").strip():
        return Detection(detector="silent_abandonment", fired=True, detail="empty reason")
    return Detection(detector="silent_abandonment", fired=False)


def detect_schema_violation(trace: RunTrace) -> Detection:
    if trace.final_decision is None:
        return Detection(detector="schema_violation", fired=False, detail="no output to check")
    try:
        Decision.model_validate(trace.final_decision)
    except ValidationError as exc:
        return Detection(detector="schema_violation", fired=True, detail=str(exc)[:200])
    return Detection(detector="schema_violation", fired=False)


def detect_excessive_budget(trace: RunTrace, max_steps: int = MAX_STEPS, max_tokens: int = MAX_TOKENS,
                            max_cost: float = MAX_COST_USD) -> Detection:
    over = []
    if len(trace.steps) > max_steps:
        over.append(f"{len(trace.steps)} steps > {max_steps}")
    if trace.total_tokens > max_tokens:
        over.append(f"{trace.total_tokens} tokens > {max_tokens}")
    if trace.total_cost_usd is not None and trace.total_cost_usd > max_cost:
        over.append(f"${trace.total_cost_usd:.4f} > ${max_cost}")
    return Detection(detector="excessive_budget", fired=bool(over), detail="; ".join(over))


DETECTORS = (detect_hallucinated_tool_args, detect_loop, detect_silent_abandonment,
             detect_schema_violation, detect_excessive_budget)
DETECTOR_NAMES = ("hallucinated_tool_args", "loop", "silent_abandonment", "schema_violation",
                  "excessive_budget")


def detect(trace: RunTrace) -> list[Detection]:
    """Run every detector on one trace."""
    return [d(trace) for d in DETECTORS]
