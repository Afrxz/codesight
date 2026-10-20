# TCG market-data MCP server

An [MCP](https://modelcontextprotocol.io) server that gives any MCP client, such
as Claude Desktop, another agent, or your own script, **Pokémon card market
data** in one normalized shape (`MarketData`).

It is the market-data layer of the TCG Portfolio & Market Agent, exposed on its
own. It wraps the project's existing price sources (JustTCG by default),
caching included, rather than re-implementing them. The agent itself can fetch
its prices through this server too (`--source mcp`).

It only reads market data. It never buys, sells or touches any account.

---

## Tools

### `get_market_data`

Today's market data for one card.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `name` | string, **required** | | Card name with collector number, e.g. `"Charizard ex 199/165"` |
| `set_code` | string | `""` | e.g. `"151"`. Optional when `catalog_id` is given |
| `grade` | string | `"Raw"` | `"Raw"` or a slab grade like `"PSA 10"` (JustTCG prices raw cards only) |
| `variant` | string, one of `normal`, `holofoil`, `reverseHolofoil`, `1stEditionHolofoil`, … | regular printing | Finish/printing (the full list is in the tool's input schema) |
| `catalog_id` | string | | Exact pokemontcg.io id, e.g. `"sv4pt5-232"` (from `search_card`); skips name matching |
| `condition` | `Near Mint`, `Lightly Played`, `Moderately Played`, `Heavily Played`, `Damaged` | `Near Mint` | Raw cards only |

**Returns** a `MarketData` object. Its JSON schema is published as the tool's
output schema. The main fields:

| Field | Meaning |
|---|---|
| `sold_avg` | Market price: the average of recent sales, or the source's market price |
| `sold_count` | How many sales back it (`null` = not reported) |
| `best_listing`, `listings` | Cheapest current listing, and sample listings |
| `sold_comps` | Sample recent sales (price, date) |
| `trend_30d_pct`, `avg_30d` | The source's 30-day change and 30-day average |
| `stats` | 7/90-day change, 30-day low/high, position in the 30-day range |
| `by_condition` | The same card's price in each condition and printing |
| `source_note` | Where the numbers came from (or why there are none) |
| `product_url` | Where to check the price yourself |

### `get_market_data_batch`

The same, for up to 100 cards in one call:
`{"cards": [{"name": ..., "grade": ...}, ...]}`.

- Batch-capable sources fetch the cards together; JustTCG does 20 cards per request, which saves quota.
- Each result is `{"query", "market", "error"}`, with either `market` or `error` set.
- One bad card doesn't fail the batch.

### `search_card`

`{"query": "zapdos 30th"}` returns matching cards from the pokemontcg.io
catalog: id, name, number, set, rarity, art and prices per finish. Pass a
result's id as `catalog_id` to `get_market_data` for an exact match.

### Errors

Errors come back as normal MCP tool errors (`isError: true`), never as a
crash. The error text contains JSON:

```json
{"code": "not_found", "message": "Couldn't find this card on JustTCG."}
```

| `code` | When |
|---|---|
| `invalid_argument` | Empty name or query, unknown `condition` or `variant`, more than 100 cards |
| `not_found` | The source has no price for this card; `message` says why |

A temporary upstream outage isn't reported as an error. Like the direct
source, you get `MarketData` with no prices and a `source_note` explaining it.

---

## Run it

All commands run from the project folder, with its virtual environment set up
(see the main [README](../README.md#1-install)).

**1. Configure.** For real prices, put a free JustTCG key in the project's
`.env` (or set it in the binder's Settings):

```
JUSTTCG_API_KEY=your-key
```

| Variable | Default | What it does |
|---|---|---|
| `MCP_SOURCE` | `justtcg` | Which source the server wraps: `justtcg`, `pokemontcg`, `tcgapi` or `mock` (offline sample data, no key) |
| `TCG_DATA_DIR` | the project's `data/` | Where caches live (the agent sets this so both share one cache) |

**2. Start it.** It speaks MCP over **stdio**, so normally your MCP client
starts it for you. Any of these launch it:

```bash
python -m mcp_server.server
```

```bash
python mcp_server/server.py
```

After `pip install -e .` (run once in the project folder) it's also a command:

```bash
tcg-mcp-server
```

Run by hand, it just waits silently for an MCP client on stdin; press Ctrl+C
to stop.

---

## Call it

### From Python (official MCP SDK)

Save this as `try_mcp.py` in the project folder and run `python try_mcp.py`:

```python
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server.server"],
        env=dict(os.environ, MCP_SOURCE="mock"),   # remove MCP_SOURCE for real JustTCG prices
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        print([tool.name for tool in (await session.list_tools()).tools])

        result = await session.call_tool("get_market_data",
                                         {"name": "Charizard ex 199/165", "grade": "PSA 10"})
        print(result.structuredContent["sold_avg"], result.structuredContent["source_note"])

        bad = await session.call_tool("get_market_data", {"name": ""})
        print(bad.isError, bad.content[0].text)


asyncio.run(main())
```

Expected output with `MCP_SOURCE="mock"` (sample data has no source note):

```
['get_market_data', 'get_market_data_batch', 'search_card']
598.0 None
True Error executing tool get_market_data: {"code": "invalid_argument", "message": "name must not be empty"}
```

### From Claude Desktop

Add the server to `claude_desktop_config.json` (Settings → Developer → Edit
Config), using your own paths, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "tcg-market-data": {
      "command": "C:\\path\\to\\TCG\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\TCG\\mcp_server\\server.py"]
    }
  }
}
```

On macOS / Linux use `/path/to/TCG/.venv/bin/python` and
`/path/to/TCG/mcp_server/server.py`. Then ask Claude something like *"What's
Charizard ex 199/165 in PSA 10 worth right now?"*.

### With the MCP Inspector (optional, needs Node.js)

```bash
npx @modelcontextprotocol/inspector python -m mcp_server.server
```

This opens a web page where you can list the tools and call them by hand.

---

## How the agent uses it

`python -m src.run --source mcp` (or **Settings → Agent runs → Market data:
mcp**) makes the agent get every price through this server.
`src/market/mcp_source.py` is a normal `MarketSource`:

- It launches the server once per run and keeps one MCP session open.
- It asks for the whole collection in a single `get_market_data_batch` call.
- It turns each result back into `MarketData`. A `not_found` error becomes the
  same "no price" result the direct source gives.
- If the server dies or times out, that's a transient error: the run retries
  the card and starts a fresh server.

The decision loop is unchanged. Because MCP only changes how prices travel,
not what they are, the run continues the wrapped source's price history (e.g.
"justtcg"). Trends, the track record and the binder's charts carry on as if the
source were used directly.

**Parity.** On a copy of a real 4-card collection, `--source mcp` and
`--source justtcg` gave identical market data and identical decisions, with no
extra API requests. The automated version of that check runs offline:

```bash
pytest tests/test_mcp_server.py -v
```

One difference to know: the LLM's optional look-up tools that read JustTCG's
raw per-condition statistics need the direct JustTCG source. Through MCP they
fall back to the price history and the card catalog. The `MarketData` each
decision is based on is identical either way.
