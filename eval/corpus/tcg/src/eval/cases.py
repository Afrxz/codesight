"""Phase 7.2 — the task suite: loading, validation, and frozen fixtures.

Cases live as JSON in src/eval/suite/ (one file per category). Each case is a
card, a *frozen* MarketData (the same contract every real source returns),
optional history and tool results, and what the graders should expect.

Determinism: nothing here touches the network or the database.
- `FixtureSource` is a real `MarketSource` that returns the case's frozen data.
- `FixtureToolbox` is the real `Toolbox` (same schemas, same tracing, same
  budget) whose look-ups answer from the case's fixtures instead of live APIs.
So the eval exercises the production fetch -> decide -> guardrails path; only
the data underneath is pinned.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..market.base import MarketSource
from ..market.types import MarketData
from ..tools import Toolbox

SUITE_DIR = Path(__file__).parent / "suite"

CATEGORIES = ("clear_buy", "clear_sell", "hold", "thin_data", "outlier_comps",
              "missing_data", "damaged_underprice")
Category = Literal["clear_buy", "clear_sell", "hold", "thin_data", "outlier_comps",
                   "missing_data", "damaged_underprice"]
Verdict = Literal["buy", "hold", "sell"]
Confidence = Literal["low", "medium", "high"]
GRADERS = ("schema", "verdict", "vs_market_math", "confidence_cap", "pnl_presence")


class CardFixture(BaseModel):
    """The card being decided, with the same fields as a `cards` row."""
    name: str
    set_code: str = "TST"
    grade: str = "Raw"
    status: Literal["watching", "owned"] = "watching"
    target_buy: float | None = None
    target_sell: float | None = None
    purchase_price: float | None = None
    purchase_date: str | None = None
    acquired: Literal["bought", "pulled", "gift"] | None = None
    condition: str | None = None
    variant: str | None = None
    notes: str | None = None


class HistoryPoint(BaseModel):
    days_ago: float
    sold_avg: float | None = None
    best_listing: float | None = None


class Expected(BaseModel):
    """What the deterministic graders check. Unset fields aren't checked."""
    verdict: Verdict | None = None
    confidence_max: Confidence | None = None


class ToolFixtures(BaseModel):
    """Frozen answers for the agent's look-ups (missing ones say 'unavailable')."""
    second_source: dict | None = None
    set_movement: dict | None = None


class EvalCase(BaseModel):
    task_id: str
    category: Category
    description: str
    card: CardFixture
    market: MarketData
    history: list[HistoryPoint] = Field(default_factory=list)
    tool_fixtures: ToolFixtures = Field(default_factory=ToolFixtures)
    expected: Expected
    graders: list[Literal["schema", "verdict", "vs_market_math", "confidence_cap", "pnl_presence"]] = \
        Field(default_factory=lambda: list(GRADERS))

    @model_validator(mode="after")
    def _expects_something(self) -> "EvalCase":
        if self.expected.verdict is None and self.expected.confidence_max is None:
            raise ValueError(f"{self.task_id}: expected must set a verdict and/or confidence_max")
        return self

    # -- the shapes the production code expects -------------------------------- #

    def card_row(self, card_id: int) -> dict:
        """The card as a `cards` row dict (what list_cards() returns), for decide()."""
        c = self.card
        return {"id": card_id, "name": c.name, "set_code": c.set_code, "grade": c.grade,
                "status": c.status, "target_buy": c.target_buy, "target_sell": c.target_sell,
                "purchase_price": c.purchase_price, "purchase_date": c.purchase_date,
                "notes": c.notes, "image_url": None, "variant": c.variant, "catalog_id": None,
                "manual_price": None, "manual_price_date": None, "acquired": c.acquired,
                "condition": c.condition}

    def history_rows(self, now: datetime | None = None) -> list[dict]:
        """History as get_history() returns it (oldest first), relative to `now`."""
        now = now or datetime.now(timezone.utc)
        rows = [{"run_at": (now - timedelta(days=h.days_ago)).isoformat(timespec="seconds"),
                 "sold_avg": h.sold_avg, "best_listing": h.best_listing, "sold_count": None}
                for h in self.history]
        return sorted(rows, key=lambda r: r["run_at"])


def load_suite(directory: Path = SUITE_DIR) -> list[EvalCase]:
    """Load and validate every case. Raises on invalid cases or duplicate ids."""
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.json")):
        for raw in json.loads(path.read_text(encoding="utf-8")):
            cases.append(EvalCase.model_validate(raw))
    ids = [c.task_id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate task ids: {sorted(duplicates)}")
    return cases


class FixtureSource(MarketSource):
    """A real MarketSource that serves one case's frozen MarketData."""

    def __init__(self, case: EvalCase):
        self.case = case

    def fetch_market_data(self, name, set_code, grade, variant=None, catalog_id=None, condition=None):
        return self.case.market.model_copy(deep=True)


class FixtureToolbox(Toolbox):
    """The real Toolbox, answering look-ups from the case's frozen fixtures."""

    case: EvalCase = None   # set by for_case

    @classmethod
    def for_case(cls, case: EvalCase, card: dict, market: MarketData, budget) -> "FixtureToolbox":
        box = cls(card=card, market=market, source_name="eval", budget=budget)
        box.case = case
        return box

    def available(self) -> list[str]:
        return ["price_history", "compare_variants", "second_source", "set_movement"]

    def _tool_price_history(self, days: int = 90):
        rows = [{"date": r["run_at"][:10], "price": r["sold_avg"]}
                for r in self.case.history_rows() if r["sold_avg"] is not None]
        stats = self.market.stats or {}
        result = {"our_snapshots": rows}
        if stats:
            result["source_stats_near_mint"] = stats
        return result, f"Price history: {len(rows)} snapshots"

    def _tool_compare_variants(self):
        rows = self.market.by_condition
        if not rows:
            return {"note": "No other conditions or printings are priced."}, "Conditions: none priced"
        return {"variants": rows}, f"Conditions: {len(rows)} priced"

    def _tool_second_source(self):
        fx = self.case.tool_fixtures.second_source
        if not fx:
            return {"note": "No second source is configured."}, "Second source: none configured"
        return fx, f"Second source: {fx.get('market_price')}"

    def _tool_set_movement(self):
        fx = self.case.tool_fixtures.set_movement
        if not fx:
            return {"note": "Set movement unavailable."}, "Set movement: unavailable"
        return fx, f"Set movement: {fx.get('median_30d_change_pct')}%"
