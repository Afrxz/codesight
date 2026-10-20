"""The `MarketSource` adapter interface.

This abstract base class is the project's single intentional abstraction. Any
data source — the deterministic mock used in development, or the live tcgapi.net
source — implements `fetch_market_data`, and the rest of the system depends only
on this contract. Swapping sources is therefore a one-line change at the call
site (see the run loop's `--source` flag), with no downstream edits.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .types import MarketData


class TransientSourceError(Exception):
    """A failure worth retrying (outage, timeout, rate limit), as opposed to a
    bad card or a bug. The run loop retries these with backoff (src/run.py)."""


class MarketSource(ABC):
    """Abstract source of normalized market data for a single card."""

    @abstractmethod
    def fetch_market_data(
        self, name: str, set_code: str, grade: str,
        variant: str | None = None, catalog_id: str | None = None,
        condition: str | None = None,
    ) -> MarketData:
        """Return a normalized `MarketData` for the given card identity.

        Implementations must degrade gracefully: an unknown card or an empty
        result should return an (essentially empty) `MarketData`, never raise.

        Args:
            name:     Card name as stored, e.g. "Charizard ex 199/165".
            set_code: Set identifier, e.g. "151".
            grade:    Grade string, e.g. "PSA 10" or "Raw" — graded and raw are
                      different markets and must not be mixed.
            variant:  Optional finish, e.g. "holofoil" or "reverseHolofoil".
                      For raw cards it's what separates markets; sources that
                      can't tell finishes apart ignore it.
            catalog_id: Optional exact pokemontcg.io id ("sv4pt5-232"). Sources
                      that can use it skip name matching; others ignore it.
            condition: For raw cards, the copy's condition ("Near Mint",
                      "Lightly Played", ...); None means Near Mint.
        """
        raise NotImplementedError

    def prefetch(self, cards) -> None:
        """Optional hook, called once with every card before a run.

        Sources whose API supports batch lookups (JustTCG: 20 cards per
        request) load everything here, so the per-card calls that follow are
        served from memory. The default does nothing.
        """
