"""Profit & loss math for owned cards.

We mark owned cards to market at the **sold average**, not the cheapest
listing: asking prices are what sellers *hope* to get, sold comps are what
buyers actually paid. That makes unrealized P&L a realistic "what could I get
for it today" figure.

    unrealized_pnl     = sold_avg - purchase_price            (dollars)
    unrealized_pnl_pct = unrealized_pnl / purchase_price * 100

Watched cards (no cost basis) always get `None`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .db import STATUS_OWNED


def compute_pnl(
    status: str, purchase_price: float | None, sold_avg: float | None
) -> tuple[float | None, float | None]:
    """Return `(unrealized_pnl, unrealized_pnl_pct)`.

    - Not owned, no cost basis recorded, or no market price: `(None, None)`;
      we never invent a price.
    - A $0 cost basis is real (a card pulled from a pack you don't count):
      the whole value is gain, so the dollar figure is the value and the
      percentage is undefined -> `(value, None)`.
    """
    if status != STATUS_OWNED or purchase_price is None or sold_avg is None:
        return None, None
    pnl = round(sold_avg - purchase_price, 2)
    pct = round(pnl / purchase_price * 100, 1) if purchase_price > 0 else None
    return pnl, pct


def portfolio_summary(decisions: Iterable[Mapping]) -> dict:
    """Aggregate owned-card decisions into the dashboard's summary strip.

    - `total_value`: sum of sold_avg over owned cards that have market data.
    - `total_cost` / `total_pnl` / `total_pnl_pct`: computed over the owned
      cards that have BOTH market data and a cost basis, so the totals always
      equal the sum of the per-card P&L figures shown in the table.
    - `best` / `worst`: the owned cards with the highest / lowest P&L %.
    """
    owned = [d for d in decisions if d["status"] == STATUS_OWNED]
    valued = [d for d in owned if d["sold_avg"] is not None]
    with_pnl = [d for d in valued if d["unrealized_pnl"] is not None]

    total_cost = round(sum(d["purchase_price"] for d in with_pnl), 2)
    total_pnl = round(sum(d["unrealized_pnl"] for d in with_pnl), 2)
    # Best/worst by % only makes sense for cards that cost something.
    ranked = sorted((d for d in with_pnl if d["unrealized_pnl_pct"] is not None),
                    key=lambda d: d["unrealized_pnl_pct"])

    return {
        "card_count": len(owned),
        "cards_with_pnl": len(with_pnl),
        "total_value": round(sum(d["sold_avg"] for d in valued), 2),
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_pnl_pct": round(total_pnl / total_cost * 100, 1) if total_cost else None,
        "best": ranked[-1] if ranked else None,
        "worst": ranked[0] if ranked else None,
    }
