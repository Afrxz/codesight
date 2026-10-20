"""Normalized market-data types — the contract the rest of the system depends on.

Every `MarketSource` (mock or live) must return a `MarketData`. Downstream code
(memory, the decision loop, the dashboard) reads *only* these models, so the
shape of a raw API response never leaks past the adapter layer.

Modeling notes worth explaining:
- `listings` / `sold_comps` are *samples* for display and evidence. The scalar
  summaries (`best_listing`, `sold_avg`, `sold_count`) describe the full market
  and are stored/trended over time. That's why `sold_count` is an independent
  field, NOT `len(sold_comps)` — a source may report "5 recent sales, avg $598"
  while only handing us one or two example comps.
- Summary fields are nullable: a thin or empty market yields `best_listing=None`
  / `sold_avg=None` / `sold_count=0` rather than a crash. If a source omits the
  summaries but provides the sample lists, we derive them (see the validator).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, NonNegativeFloat, NonNegativeInt, model_validator


class Listing(BaseModel):
    """A single current 'for sale' listing."""

    price: NonNegativeFloat
    condition: str  # e.g. 'PSA 10', 'PSA 9', 'Raw/NM' — graded vs raw matters
    url: str = ""   # link to the listing; plain str (sources sometimes omit it)


class SoldComp(BaseModel):
    """A single recent completed/sold sale ('comp')."""

    price: NonNegativeFloat
    date: date  # when it sold (ISO date, e.g. 2026-09-24)


class MarketData(BaseModel):
    """Normalized snapshot of a card's market at one point in time."""

    best_listing: NonNegativeFloat | None = None  # cheapest current listing
    listings: list[Listing] = Field(default_factory=list)
    sold_comps: list[SoldComp] = Field(default_factory=list)
    sold_avg: NonNegativeFloat | None = None      # avg of recent sold prices
    # How many sales back sold_avg. None = the source doesn't say (e.g. an
    # aggregated market price) — which is different from 0 sales.
    sold_count: NonNegativeInt | None = 0
    # Where these numbers came from — or, when there are none, why — shown in
    # the UI (e.g. "TCGplayer Holofoil prices via pokemontcg.io, updated 2026-10-15").
    source_note: str | None = None
    # True when sold_avg is the owner's own estimate rather than market data
    # (see run.with_manual_price). The decision layer treats it as low confidence.
    manual: bool = False
    # The source's own 30-day price change in %, when it reports one (JustTCG
    # does). Gives a trend before our own history has two points.
    trend_30d_pct: float | None = None
    # The source's 30-day average price, when reported (JustTCG). Lets the agent
    # spot dips ("22% below its 30-day average") without listing prices.
    avg_30d: float | None = None
    # The same card's price in each condition/printing (JustTCG), for the
    # "prices by condition" table: [{"condition", "printing", "price", "change_30d"}].
    by_condition: list[dict] = Field(default_factory=list)
    # Where you can check the price yourself (the TCGplayer product page).
    product_url: str | None = None
    # The source's own price statistics, when reported (JustTCG): 7/90-day change,
    # the 30-day low/high, and where today's price sits in that range (0 = at the
    # low, 1 = at the high). Part of the basic facts, so every decision path sees them.
    stats: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _derive_missing_summaries(self) -> "MarketData":
        """Fill summary fields from the sample lists when a source omits them.

        This is a convenience so simple/partial sources don't have to compute
        them, while sources that DO provide authoritative summaries (and a
        larger true `sold_count`) keep theirs untouched.
        """
        if self.best_listing is None and self.listings:
            self.best_listing = min(x.price for x in self.listings)
        if self.sold_avg is None and self.sold_comps:
            self.sold_avg = round(
                sum(x.price for x in self.sold_comps) / len(self.sold_comps), 2
            )
        # Only backfill sold_count from the sample when it wasn't given at all.
        if self.sold_count == 0 and self.sold_comps:
            self.sold_count = len(self.sold_comps)
        return self

    @property
    def is_thin(self) -> bool:
        """True when the sold data is known to be too sparse to trust (<= 2 comps).

        Used by the decision layer to force low confidence. An unknown count
        isn't "thin" — the decision layer caps that case at medium instead.
        """
        return self.sold_count is not None and self.sold_count <= 2
