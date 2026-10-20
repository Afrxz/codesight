"""Free market source: TCGplayer prices for raw cards, via pokemontcg.io.

What pokemontcg.io returns (checked against a real response, 2026-10):

    "tcgplayer": {
      "url": "https://prices.pokemontcg.io/tcgplayer/sv4pt5-232",
      "updatedAt": "2026/10/15",
      "prices": {
        "holofoil": {"low": 729.99, "mid": 933.05, "high": 10000.0,
                     "market": 863.75, "directLow": 820.0}
      }
    }

One price block per *finish* ("normal", "holofoil", "reverseHolofoil",
"1stEditionHolofoil", ...), which is exactly the variant that separates raw
markets. How it maps onto MarketData:

- sold_avg     <- `market`: TCGplayer Market Price, derived from recent sales.
- best_listing <- `directLow`: the cheapest TCGplayer Direct listing, which is
                  always Near Mint. We prefer it over `low` because `low` can be
                  any condition — a damaged copy would fake a "30% under market"
                  buy signal. `low` is only used when `directLow` is missing,
                  and is labelled as such.
- sold_count   <- None: no sales count is reported, so the decision layer caps
                  confidence at "medium" for this source.
- sold_comps   <- []: no individual sales; trend comes from our own history.

Deliberately ignored: the `cardmarket` block (EUR, and noticeably stale in
practice) and `high` (dominated by joke listings like $10,000).

Raw cards only: TCGplayer prices are for ungraded copies, so graded cards
return an empty MarketData rather than a misleading raw price.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta

from .. import config
from ..pokemontcg import CARD_FIELDS, find_card, search
from .base import MarketSource
from .tcgapi import is_raw
from .types import Listing, MarketData

log = logging.getLogger(__name__)

# Finish keys as pokemontcg.io names them, with display labels. When a card
# has several and none was chosen, the first one present in this order wins:
# the regular (non-reverse) printing is what people usually mean.
VARIANTS: dict[str, str] = {
    "holofoil": "Holofoil",
    "normal": "Normal",
    "unlimitedHolofoil": "Unlimited Holofoil",
    "unlimited": "Unlimited",
    "reverseHolofoil": "Reverse Holofoil",
    "1stEditionHolofoil": "1st Edition Holofoil",
    "1stEditionNormal": "1st Edition",
}


def choose_variant(prices: dict, wanted: str | None) -> str | None:
    """Pick which finish's prices to use, or None if the wanted one isn't priced."""
    if wanted:
        return wanted if wanted in prices else None   # never silently swap finishes
    for key in VARIANTS:
        if key in prices:
            return key
    return next(iter(prices), None)   # an unexpected key: better than nothing


class PokemonTcgSource(MarketSource):
    """`MarketSource` for raw cards using pokemontcg.io's TCGplayer prices."""

    def __init__(self, cache_ttl: timedelta | None = None, session=None,
                 sleep: Callable[[float], None] = time.sleep):
        # Prices refresh about daily upstream; reuse the "Live data" cache setting.
        self.cache_ttl = cache_ttl if cache_ttl is not None else timedelta(hours=config.get("CACHE_TTL_HOURS"))
        self.session = session
        self.sleep = sleep

    def fetch_market_data(self, name: str, set_code: str, grade: str,
                          variant: str | None = None, catalog_id: str | None = None,
                          condition: str | None = None) -> MarketData:
        try:
            market = self._fetch(name, grade, variant, catalog_id)
            # TCGplayer prices here are Near Mint only; say so for other conditions.
            if condition and condition != "Near Mint" and market.sold_avg is not None:
                market = market.model_copy(update={"source_note": (
                    f"{market.source_note}. These are Near Mint prices; pokemontcg.io has none for "
                    f"{condition}, so your copy is likely worth less (JustTCG prices each condition).")})
            return market
        except Exception:
            log.exception("pokemontcg.io fetch failed for %s", name)   # contract: never raise
            return MarketData()

    def _fetch(self, name: str, grade: str, variant: str | None, catalog_id: str | None) -> MarketData:
        # Every "no data" outcome carries a source_note saying why, which the
        # binder shows instead of a generic "no data".
        if not is_raw(grade):
            log.info("Skipping %s (%s): pokemontcg.io only has raw-card prices", name, grade)
            return MarketData(source_note="pokemontcg.io only has prices for raw (ungraded) cards.")

        if catalog_id:
            # Added from the binder's search: we know the exact printing.
            cards = search(f"id:{catalog_id}", CARD_FIELDS, ttl=self.cache_ttl,
                           session=self.session, sleep=self.sleep)
            if cards is None:
                return MarketData(source_note="pokemontcg.io didn't respond; the next run will try again.")
            card = (cards or [None])[0]
        else:
            card = find_card(name, select="id,name,tcgplayer", ttl=self.cache_ttl,
                             session=self.session, sleep=self.sleep)
        if card is None:
            return MarketData(source_note="Couldn't find this printing on pokemontcg.io "
                                          "(or it didn't respond).")
        tcgplayer = card.get("tcgplayer") or {}
        prices = tcgplayer.get("prices") or {}
        key = choose_variant(prices, variant)
        if key is None:
            log.info("No TCGplayer prices for %s%s", name, f" ({variant})" if variant else "")
            if prices and variant:
                have = ", ".join(VARIANTS.get(k, k) for k in prices)
                note = (f"pokemontcg.io has no {VARIANTS.get(variant, variant)} price for this card "
                        f"(it has: {have}).")
            else:
                note = "pokemontcg.io has no TCGplayer price for this card yet (new sets can take a while)."
            return MarketData(source_note=note)

        block = prices[key]
        label = VARIANTS.get(key, key)
        market, direct_low, low = block.get("market"), block.get("directLow"), block.get("low")
        listing_price = direct_low if direct_low is not None else low
        listings = []
        if listing_price is not None:
            kind = "TCGplayer Direct, Near Mint" if direct_low is not None else "TCGplayer low, any condition"
            listings.append(Listing(price=listing_price, condition=f"Raw · {label} ({kind})",
                                    url=tcgplayer.get("url", "")))

        updated = tcgplayer.get("updatedAt")
        return MarketData(
            best_listing=listing_price,
            listings=listings,
            sold_comps=[],
            sold_avg=market,
            sold_count=None,
            source_note=f"TCGplayer {label} prices via pokemontcg.io"
                        + (f", updated {updated.replace('/', '-')}" if updated else ""),
            product_url=tcgplayer.get("url") or None,   # redirects to the TCGplayer product page
        )
