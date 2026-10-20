"""The agent's market-data layer as a standalone MCP server (Layer C).

    python -m mcp_server.server          # stdio transport (what MCP clients launch)

No new ingestion code: it wraps the project's existing MarketSource (JustTCG by
default, set MCP_SOURCE to change it) and returns exactly the normalized
`MarketData` the agent already uses. The agent's own McpSource
(src/market/mcp_source.py) talks to it, and so can any other MCP client.

Contract (Phase 9.2):
- Inputs are typed and described. Allowed values (grade, condition, finish)
  are listed in each tool's input schema.
- `get_market_data` declares MarketData's JSON schema as its output schema, so
  clients can validate what they get.
- Errors are structured tool errors, never crashes: the error text is JSON
  {"code", "message"} with code
      invalid_argument   bad input (empty name, unknown condition, ...)
      not_found          the source has no price for this card (message says why)
  An upstream outage isn't an error here: like the direct source, it returns
  MarketData with a note, so the agent behaves the same either way.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

# Launched by path (e.g. from Claude Desktop's config) the project root isn't on
# sys.path, so `src` wouldn't import; add it. Harmless under `python -m`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src import config  # noqa: E402
from src.market.types import MarketData  # noqa: E402
from src.market.pokemontcg_source import VARIANTS  # noqa: E402

# The agent passes its own data folder, so both share one set of caches.
if os.environ.get("TCG_DATA_DIR"):
    config.DATA_DIR = Path(os.environ["TCG_DATA_DIR"])

CONDITIONS = ("Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged")
MAX_BATCH = 100

mcp = FastMCP("tcg-market-data", instructions=(
    "Pokémon card market data (market price, listings, sold comps, prices by condition), "
    "normalized to one MarketData shape. Data only: it never buys or sells anything."))

_source = None


def source():
    """The wrapped MarketSource, built once per server process (keeps its caches warm)."""
    global _source
    if _source is None:
        from src.run import make_source
        _source = make_source(config.get("MCP_SOURCE"))
    return _source


# --- input types (they become the tools' JSON input schemas) ---------------------

Name = Annotated[str, Field(description='Card name with its collector number, e.g. "Charizard ex 199/165".')]
SetCode = Annotated[str, Field(description='Set code, e.g. "151". Optional when catalog_id is given.')]
Grade = Annotated[str, Field(description='"Raw" (ungraded) or a slab grade such as "PSA 10". '
                                         "Raw and graded are different markets.")]
Variant = Annotated[str | None, Field(description="Finish/printing (default: the regular printing).",
                                      json_schema_extra={"enum": [*VARIANTS, None]})]
CatalogId = Annotated[str | None, Field(description='Exact pokemontcg.io card id, e.g. "sv4pt5-232" '
                                                    "(skips name matching).")]
Condition = Annotated[str | None, Field(description="Raw cards' condition (default Near Mint).",
                                        json_schema_extra={"enum": [*CONDITIONS, None]})]


class CardQuery(BaseModel):
    """One card to look up (the same identity the agent stores per card)."""

    name: Name
    set_code: SetCode = ""
    grade: Grade = "Raw"
    variant: Variant = None
    catalog_id: CatalogId = None
    condition: Condition = None


class ErrorInfo(BaseModel):
    code: str = Field(description="invalid_argument | not_found")
    message: str


class BatchItem(BaseModel):
    """One result of get_market_data_batch: market data, or why there is none."""

    query: CardQuery
    market: MarketData | None = None
    error: ErrorInfo | None = None


class BatchResult(BaseModel):
    results: list[BatchItem]


# --- the work, shared by the tools ------------------------------------------------

class _Problem(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _validate(q: CardQuery) -> None:
    if not q.name.strip():
        raise _Problem("invalid_argument", "name must not be empty")
    if q.condition is not None and q.condition not in CONDITIONS:
        raise _Problem("invalid_argument", f"condition must be one of: {', '.join(CONDITIONS)}")
    if q.variant is not None and q.variant not in VARIANTS:
        raise _Problem("invalid_argument", f"variant must be one of: {', '.join(VARIANTS)}")


def _lookup(q: CardQuery) -> MarketData:
    """Validated query -> MarketData; raises _Problem("not_found") when there's no price."""
    _validate(q)
    market = source().fetch_market_data(q.name, q.set_code, q.grade or "Raw", q.variant,
                                        q.catalog_id, q.condition)
    if market.sold_avg is None and market.best_listing is None:
        raise _Problem("not_found", market.source_note or "No market data for this card.")
    return market


def _tool_error(problem: _Problem) -> ToolError:
    return ToolError(json.dumps({"code": problem.code, "message": problem.message}))


# --- tools ---------------------------------------------------------------------------

@mcp.tool()
def get_market_data(name: Name, set_code: SetCode = "", grade: Grade = "Raw", variant: Variant = None,
                    catalog_id: CatalogId = None, condition: Condition = None) -> MarketData:
    """Today's normalized market data for one Pokémon card: market (sold) price, cheapest
    listing, recent sales, 30-day change and average, and prices by condition."""
    try:
        return _lookup(CardQuery(name=name, set_code=set_code, grade=grade, variant=variant,
                                 catalog_id=catalog_id, condition=condition))
    except _Problem as problem:
        raise _tool_error(problem) from None


@mcp.tool()
def get_market_data_batch(
        cards: Annotated[list[CardQuery], Field(description=f"Up to {MAX_BATCH} cards.")]) -> BatchResult:
    """Market data for many cards in one call. Batch-capable sources (JustTCG: 20 cards per
    request) then fetch them together, which saves API quota. Each result has either
    `market` or `error`; one bad card doesn't fail the batch."""
    if len(cards) > MAX_BATCH:
        raise _tool_error(_Problem("invalid_argument", f"at most {MAX_BATCH} cards per batch"))
    valid = []
    for q in cards:
        try:
            _validate(q)
            valid.append(q)
        except _Problem:
            pass
    source().prefetch([{"name": q.name, "set_code": q.set_code, "grade": q.grade or "Raw",
                        "variant": q.variant, "catalog_id": q.catalog_id, "condition": q.condition}
                       for q in valid])
    results = []
    for q in cards:
        try:
            results.append(BatchItem(query=q, market=_lookup(q)))
        except _Problem as problem:
            results.append(BatchItem(query=q, error=ErrorInfo(code=problem.code, message=problem.message)))
    return BatchResult(results=results)


@mcp.tool()
def search_card(query: Annotated[str, Field(description='e.g. "zapdos 30th", "charizard ex", "199/165"')]
                ) -> list[dict]:
    """Find cards in the Pokémon catalog (pokemontcg.io): id, name, number, set, rarity,
    art and current prices per finish. Use the id as catalog_id in get_market_data."""
    if not query.strip():
        raise _tool_error(_Problem("invalid_argument", "query must not be empty"))
    from src import catalog
    return catalog.search(query)


def main() -> None:
    mcp.run()   # stdio


if __name__ == "__main__":
    main()
