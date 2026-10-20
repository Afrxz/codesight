"""Phase 7.3 — graders: how "correct" is defined for a fuzzy decision.

Deterministic graders (these decide pass/fail):

    schema          the output is a valid Decision
    verdict         verdict == expected verdict (only for cases that set one;
                    ambiguous cases set only a confidence cap instead)
    vs_market_math  the numbers come from the data, not the model: best_listing
                    and sold_avg equal the frozen fixture, and vs_market_pct is
                    (best_listing - sold_avg) / sold_avg * 100, to 0.1
    confidence_cap  confidence is no higher than the case allows (thin data)
    pnl_presence    P&L follows the project's rules: watched cards have none;
                    owned cards with a known cost and a price have the dollar
                    figure (= sold_avg - cost); the % only when the cost is > 0
                    (a pulled card's $0 cost makes a percentage meaningless)

A case passes only if every *applicable* grader passes. A grader that doesn't
apply to a case (no expected verdict, nothing to cap) is reported as such and
doesn't count either way.

LLM-as-judge (optional, `judge`): a rubric score for whether the reason cites
the real numbers and justifies the verdict. It's a separate, clearly labelled
metric and never affects pass/fail — a fuzzy judge must not gate the
deterministic result.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from ..decide import Decision, vs_market_pct
from .cases import EvalCase

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
MATH_TOLERANCE = 0.11      # vs_market_pct is rounded to 0.1 in the agent


class GradeResult(BaseModel):
    grader: str
    applicable: bool = True
    passed: bool
    reason: str


class CaseGrade(BaseModel):
    task_id: str
    passed: bool
    results: list[GradeResult]
    judge: dict | None = None        # LLM-as-judge scores, if run (never affects `passed`)

    @property
    def failures(self) -> list[GradeResult]:
        return [r for r in self.results if r.applicable and not r.passed]


def _na(grader: str, why: str) -> GradeResult:
    return GradeResult(grader=grader, applicable=False, passed=True, reason=f"not applicable: {why}")


def grade_schema(case: EvalCase, decision: dict | None) -> GradeResult:
    if decision is None:
        return GradeResult(grader="schema", passed=False, reason="no decision was produced")
    try:
        Decision.model_validate(decision)
    except ValidationError as exc:
        return GradeResult(grader="schema", passed=False, reason=f"invalid Decision: {str(exc)[:200]}")
    return GradeResult(grader="schema", passed=True, reason="valid Decision")


def grade_verdict(case: EvalCase, decision: dict | None) -> GradeResult:
    want = case.expected.verdict
    if want is None:
        return _na("verdict", "ambiguous case, no single right verdict")
    got = (decision or {}).get("verdict")
    return GradeResult(grader="verdict", passed=got == want,
                       reason=f"expected {want}, got {got}")


def grade_vs_market_math(case: EvalCase, decision: dict | None) -> GradeResult:
    if decision is None:
        return GradeResult(grader="vs_market_math", passed=False, reason="no decision")
    m = case.market
    problems = []
    if decision.get("best_listing") != m.best_listing:
        problems.append(f"best_listing {decision.get('best_listing')} != data {m.best_listing}")
    if decision.get("sold_avg") != m.sold_avg:
        problems.append(f"sold_avg {decision.get('sold_avg')} != data {m.sold_avg}")
    expected = vs_market_pct(m.best_listing, m.sold_avg)
    got = decision.get("vs_market_pct")
    if (expected is None) != (got is None) or (
            expected is not None and abs(expected - got) > MATH_TOLERANCE):
        problems.append(f"vs_market_pct {got} != recomputed {expected}")
    return GradeResult(grader="vs_market_math", passed=not problems,
                       reason="; ".join(problems) or f"numbers match the data (vs_market {got})")


def grade_confidence_cap(case: EvalCase, decision: dict | None) -> GradeResult:
    cap = case.expected.confidence_max
    if cap is None:
        return _na("confidence_cap", "no cap for this case")
    got = (decision or {}).get("confidence")
    ok = got is not None and CONFIDENCE_RANK[got] <= CONFIDENCE_RANK[cap]
    return GradeResult(grader="confidence_cap", passed=ok, reason=f"cap {cap}, got {got}")


def grade_pnl_presence(case: EvalCase, decision: dict | None) -> GradeResult:
    if decision is None:
        return GradeResult(grader="pnl_presence", passed=False, reason="no decision")
    pnl, pct = decision.get("unrealized_pnl"), decision.get("unrealized_pnl_pct")
    card, price = case.card, case.market.sold_avg
    if card.status == "watching":
        ok = pnl is None and pct is None
        return GradeResult(grader="pnl_presence", passed=ok,
                           reason="watched card has no P&L" if ok else f"watched card has P&L {pnl}/{pct}")
    if card.purchase_price is None or price is None:
        ok = pnl is None and pct is None
        return GradeResult(grader="pnl_presence", passed=ok,
                           reason="no cost or no price -> no P&L" if ok else f"unexpected P&L {pnl}/{pct}")
    want_pnl = round(price - card.purchase_price, 2)
    problems = []
    if pnl is None or abs(pnl - want_pnl) > 0.01:
        problems.append(f"unrealized_pnl {pnl} != {want_pnl}")
    if card.purchase_price > 0 and pct is None:
        problems.append("unrealized_pnl_pct missing although there's a cost")
    if card.purchase_price == 0 and pct is not None:
        problems.append("unrealized_pnl_pct present although the cost is $0")
    return GradeResult(grader="pnl_presence", passed=not problems,
                       reason="; ".join(problems) or f"P&L {pnl} / {pct}% as expected")


GRADER_FUNCS = {
    "schema": grade_schema,
    "verdict": grade_verdict,
    "vs_market_math": grade_vs_market_math,
    "confidence_cap": grade_confidence_cap,
    "pnl_presence": grade_pnl_presence,
}


def grade(case: EvalCase, decision: dict | None) -> CaseGrade:
    """Apply the case's deterministic graders."""
    results = [GRADER_FUNCS[name](case, decision) for name in case.graders]
    passed = all(r.passed for r in results if r.applicable)
    return CaseGrade(task_id=case.task_id, passed=passed, results=results)


# --- Optional LLM-as-judge (separate metric) ------------------------------------- #

class JudgeScore(BaseModel):
    cites_numbers: int = Field(ge=0, le=2)     # 0 none, 1 some, 2 the key numbers, all correct
    justified: int = Field(ge=0, le=2)         # 0 doesn't follow, 1 partly, 2 clearly justifies
    note: str = Field(default="", max_length=300)


JUDGE_PROMPT = """\
You grade the written reason behind one buy/hold/sell recommendation for a
Pokémon card. You get the market facts and the decision as JSON.

Score two things, 0-2 each:
- cites_numbers: 0 = no real numbers; 1 = some numbers, or any number that
  doesn't match the facts; 2 = cites the key numbers (prices, % vs market,
  trend or P&L) and all of them match the facts.
- justified: 0 = the reason doesn't support the verdict; 1 = partly;
  2 = the reason clearly and correctly justifies the verdict.

Respond with JSON only: {"cites_numbers": 0-2, "justified": 0-2, "note": "short"}
"""


def judge(case: EvalCase, decision: dict | None, llm) -> dict | None:
    """Rubric score for the reason (None if there's no decision or the judge fails)."""
    if decision is None or llm is None:
        return None
    from ..decide import _extract_json
    payload = {"facts": case.market.model_dump(mode="json"), "card": case.card.model_dump(),
               "decision": {k: decision.get(k) for k in ("verdict", "confidence", "reason")}}
    try:
        score = JudgeScore.model_validate(_extract_json(llm(JUDGE_PROMPT, json.dumps(payload))))
    except Exception:
        return None
    return score.model_dump()
