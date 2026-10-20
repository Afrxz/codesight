"""Deterministic mock market source for development.

`MockSource` returns hand-authored, fixed `MarketData` for each seeded card so
the entire pipeline (memory -> decision -> dashboard) can be built and tested
without ever touching the live API. The canned data is chosen to exercise every
decision branch:

- **Clear under-market BUY:** Charizard ex 199/165 — cheapest listing far below
  the sold average (~31% under).
- **Over-market SELL (owned):** Charizard VMAX 020/189 — market has run well
  above cost basis and past the sell target.
- **Thin-data case:** Mew ex 232/091 (Raw) — only 2 sold comps, so the decision
  layer should lower confidence.
- Plus several middling HOLD cases.

Data is keyed on (name, grade): graded and raw are different markets, so a query
for the same card at a different grade intentionally misses and returns an empty
`MarketData` — the same graceful-degradation path the live source will use.
"""

from __future__ import annotations

import math
from datetime import date

from .base import MarketSource
from .types import MarketData


# Canned payloads keyed by (card name, grade). Values are plain dicts validated
# into MarketData on the way out, exactly as a real API response would be.
_CANNED: dict[tuple[str, str], dict] = {
    # --- Watching ---
    # Clear BUY: listing $412 vs sold avg $598 => ~31% under market.
    ("Charizard ex 199/165", "PSA 10"): {
        "best_listing": 412.00,
        "listings": [
            {"price": 412.00, "condition": "PSA 10", "url": "https://mock/chzex/1"},
            {"price": 449.99, "condition": "PSA 10", "url": "https://mock/chzex/2"},
            {"price": 475.00, "condition": "PSA 10", "url": "https://mock/chzex/3"},
        ],
        "sold_comps": [
            {"price": 560.00, "date": "2026-09-10"},
            {"price": 598.00, "date": "2026-09-24"},
            {"price": 625.00, "date": "2026-10-01"},
        ],
        "sold_avg": 598.00,
        "sold_count": 5,
    },
    # HOLD: listing roughly at / slightly above sold avg.
    ("Umbreon VMAX 215/203", "PSA 10"): {
        "best_listing": 520.00,
        "listings": [
            {"price": 520.00, "condition": "PSA 10", "url": "https://mock/umbreon/1"},
            {"price": 540.00, "condition": "PSA 10", "url": "https://mock/umbreon/2"},
        ],
        "sold_comps": [
            {"price": 515.00, "date": "2026-09-12"},
            {"price": 505.00, "date": "2026-09-22"},
            {"price": 495.00, "date": "2026-10-02"},
        ],
        "sold_avg": 505.00,
        "sold_count": 6,
    },
    # HOLD/AVOID: cheapest listing is ABOVE the sold average (no edge to buy).
    ("Pikachu V 157/172", "PSA 10"): {
        "best_listing": 175.00,
        "listings": [
            {"price": 175.00, "condition": "PSA 10", "url": "https://mock/pika/1"},
            {"price": 189.00, "condition": "PSA 10", "url": "https://mock/pika/2"},
        ],
        "sold_comps": [
            {"price": 155.00, "date": "2026-09-08"},
            {"price": 150.00, "date": "2026-09-20"},
            {"price": 146.00, "date": "2026-10-03"},
        ],
        "sold_avg": 150.00,
        "sold_count": 4,
    },
    # THIN DATA: only 2 sold comps -> is_thin == True -> lower confidence.
    ("Mew ex 232/091", "Raw"): {
        "best_listing": 95.00,
        "listings": [
            {"price": 95.00, "condition": "Raw/NM", "url": "https://mock/mew/1"},
            {"price": 130.00, "condition": "Raw/NM", "url": "https://mock/mew/2"},
        ],
        "sold_comps": [
            {"price": 100.00, "date": "2026-09-05"},
            {"price": 120.00, "date": "2026-09-28"},
        ],
        "sold_avg": 110.00,
        "sold_count": 2,
    },
    # --- Owned ---
    # Clear SELL: market ~$365 vs cost basis $180, and above the $350 sell target.
    ("Charizard VMAX 020/189", "PSA 10"): {
        "best_listing": 355.00,
        "listings": [
            {"price": 355.00, "condition": "PSA 10", "url": "https://mock/chzvmax/1"},
            {"price": 379.00, "condition": "PSA 10", "url": "https://mock/chzvmax/2"},
        ],
        "sold_comps": [
            {"price": 330.00, "date": "2026-09-06"},
            {"price": 360.00, "date": "2026-09-19"},
            {"price": 405.00, "date": "2026-10-02"},
        ],
        "sold_avg": 365.00,
        "sold_count": 7,
    },
    # HOLD (owned): small gain over cost basis, no strong signal.
    ("Lugia V 186/195", "PSA 9"): {
        "best_listing": 115.00,
        "listings": [
            {"price": 115.00, "condition": "PSA 9", "url": "https://mock/lugia/1"},
            {"price": 125.00, "condition": "PSA 9", "url": "https://mock/lugia/2"},
        ],
        "sold_comps": [
            {"price": 118.00, "date": "2026-09-11"},
            {"price": 120.00, "date": "2026-09-25"},
            {"price": 122.00, "date": "2026-10-01"},
        ],
        "sold_avg": 120.00,
        "sold_count": 5,
    },
    # HOLD (owned): up on cost basis but still under the $400 sell target.
    ("Giratina V 186/196", "PSA 10"): {
        "best_listing": 300.00,
        "listings": [
            {"price": 300.00, "condition": "PSA 10", "url": "https://mock/gira/1"},
            {"price": 315.00, "condition": "PSA 10", "url": "https://mock/gira/2"},
        ],
        "sold_comps": [
            {"price": 285.00, "date": "2026-09-09"},
            {"price": 290.00, "date": "2026-09-23"},
            {"price": 295.00, "date": "2026-10-04"},
        ],
        "sold_avg": 290.00,
        "sold_count": 4,
    },
}


# Per-card daily price drift (fraction per day) used only when `as_of` is in
# the past, so demo history tells the same story as today's data: Charizard ex
# and the owned cards have been climbing, Umbreon and Pikachu softening.
_DRIFT_PER_DAY: dict[str, float] = {
    "Charizard ex 199/165": 0.006,
    "Umbreon VMAX 215/203": -0.002,
    "Pikachu V 157/172": -0.003,
    "Mew ex 232/091": 0.001,
    "Charizard VMAX 020/189": 0.008,
    "Lugia V 186/195": 0.002,
    "Giratina V 186/196": 0.003,
}


def _price_factor(name: str, days_ago: int) -> float:
    """Deterministic multiplier for a past date: a trend plus a gentle wiggle.

    days_ago == 0 always gives exactly 1.0, so today's canned data is unchanged.
    """
    if days_ago <= 0:
        return 1.0
    phase = sum(map(ord, name)) % 7          # stable per-card offset, no randomness
    wiggle = 0.015 * math.sin(days_ago * 0.9 + phase)
    return max(0.1, 1 - _DRIFT_PER_DAY.get(name, 0.0) * days_ago + wiggle)


class MockSource(MarketSource):
    """A `MarketSource` that serves fixed, deterministic data for seeded cards.

    Pass `as_of` (a past date) to get the same card's prices "as they were" that
    day — used by `src.demo` to backfill believable history. Omit it for today.
    """

    def __init__(self, as_of: date | None = None):
        self.as_of = as_of

    def fetch_market_data(self, name: str, set_code: str, grade: str,
                          variant: str | None = None, catalog_id: str | None = None,
                          condition: str | None = None) -> MarketData:
        # Match on (name, grade). A miss (unknown card, or a grade we don't have
        # canned data for) returns an empty MarketData — never raises — mirroring
        # how the live source must degrade on an unknown card.
        payload = _CANNED.get((name, grade))
        if payload is None:
            return MarketData(source_note="The mock source only has sample data for the demo cards; "
                                          "switch Market data to a real source in Settings.")
        market = MarketData.model_validate(payload)
        if self.as_of is None:
            return market

        f = _price_factor(name, (date.today() - self.as_of).days)

        def scale(price: float) -> float:
            return round(price * f, 2)

        return market.model_copy(update={
            "best_listing": scale(market.best_listing),
            "sold_avg": scale(market.sold_avg),
            "listings": [x.model_copy(update={"price": scale(x.price)}) for x in market.listings],
            "sold_comps": [x.model_copy(update={"price": scale(x.price)}) for x in market.sold_comps],
        })
