"""Layer C (offline, mock upstream): the MCP server and the agent's McpSource.

9.1  the server starts over stdio, lists its tools, answers get_market_data
9.2  precise schemas; bad input and unknown cards come back as structured errors
9.3  the agent run through McpSource makes the same decisions as the direct source
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.market.types import MarketData

ROOT = Path(__file__).resolve().parent.parent
GOOD = {"name": "Charizard ex 199/165", "set_code": "151", "grade": "PSA 10"}


def error_payload(result) -> dict:
    """The structured {"code", "message"} inside an isError result (the SDK prefixes some text)."""
    text = result.content[0].text
    return json.loads(text[text.index("{"):])


async def _with_server(work):
    env = dict(os.environ, MCP_SOURCE="mock", PYTHONIOENCODING="utf-8")
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"],
                                   cwd=str(ROOT), env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return await work(session)


@pytest.fixture(scope="module")
def server_results():
    async def work(session):
        tools = {t.name: t for t in (await session.list_tools()).tools}
        calls = {
            "good": await session.call_tool("get_market_data", GOOD),
            "unknown": await session.call_tool("get_market_data", {"name": "Not A Card 1/1", "grade": "PSA 10"}),
            "empty": await session.call_tool("get_market_data", {"name": "  "}),
            "bad_condition": await session.call_tool("get_market_data", {"name": "Pikachu 58/102",
                                                                         "condition": "Mint-ish"}),
            "batch": await session.call_tool("get_market_data_batch", {"cards": [
                GOOD, {"name": "Not A Card 1/1", "grade": "PSA 10"}, {"name": ""}]}),
        }
        return tools, calls
    return asyncio.run(_with_server(work))


# --- 9.1 -------------------------------------------------------------------------

def test_server_lists_tools_and_answers(server_results):
    tools, calls = server_results
    assert {"get_market_data", "get_market_data_batch", "search_card"} <= set(tools)
    good = calls["good"]
    assert not good.isError
    market = MarketData.model_validate(good.structuredContent)        # schema-valid MarketData
    assert market.sold_avg == 598.0 and market.listings


# --- 9.2 -------------------------------------------------------------------------

def test_tool_schemas_are_precise(server_results):
    tools, _ = server_results
    t = tools["get_market_data"]
    props = t.inputSchema["properties"]
    assert t.inputSchema["required"] == ["name"]
    assert "Near Mint" in json.dumps(props["condition"]) and "reverseHolofoil" in json.dumps(props["variant"])
    assert all(props[k].get("description") for k in ("name", "grade", "condition"))
    # The output schema is the agent's own MarketData contract.
    assert set(MarketData.model_json_schema()["properties"]) <= set(t.outputSchema["properties"])


def test_errors_are_structured(server_results):
    _, calls = server_results
    assert error_payload(calls["unknown"])["code"] == "not_found"
    assert error_payload(calls["empty"])["code"] == "invalid_argument"
    assert error_payload(calls["bad_condition"])["code"] == "invalid_argument"
    assert all(calls[k].isError for k in ("unknown", "empty", "bad_condition"))


def test_batch_reports_each_card(server_results):
    _, calls = server_results
    good, unknown, empty = calls["batch"].structuredContent["results"]
    assert MarketData.model_validate(good["market"]).sold_avg == 598.0 and good["error"] is None
    assert unknown["error"]["code"] == "not_found" and unknown["market"] is None
    assert empty["error"]["code"] == "invalid_argument"


# --- 9.3: the agent through McpSource -------------------------------------------

def test_mcp_source_matches_the_direct_source(data_dir, monkeypatch):
    from src.manage import list_cards
    from src.market.mcp_source import McpSource
    from src.market.mock import MockSource

    monkeypatch.setenv("MCP_SOURCE", "mock")
    cards = list_cards()
    via_mcp = McpSource()
    try:
        via_mcp.prefetch(cards)
        for c in cards:
            args = (c["name"], c["set_code"], c["grade"], c["variant"], c["catalog_id"], c["condition"])
            assert via_mcp.fetch_market_data(*args) == MockSource().fetch_market_data(*args)
        unknown = via_mcp.fetch_market_data("Not A Card 1/1", "", "PSA 10")   # not prefetched: one call
        assert unknown == MockSource().fetch_market_data("Not A Card 1/1", "", "PSA 10")
        with pytest.raises(ValueError, match="rejected"):
            via_mcp.fetch_market_data("Pikachu 58/102", "BS", "Raw", condition="Mint-ish")
    finally:
        via_mcp.close()


def test_run_through_mcp_makes_the_same_decisions(data_dir, monkeypatch):
    from src import ledger
    from src.memory import get_history
    from src.run import run

    monkeypatch.setenv("MCP_SOURCE", "mock")
    direct = run("mock", "off", out_path=data_dir / "direct.json")
    # Same price series, so clear the direct run's snapshots: both runs then decide from
    # identical inputs (a first run, no history), and any difference is the transport's.
    from src.db import get_connection
    with get_connection() as conn:
        conn.execute("DELETE FROM price_history")
    through = run("mcp", "off", out_path=data_dir / "mcp.json")

    fields = ("card_id", "verdict", "confidence", "reason", "best_listing", "sold_avg", "sold_count",
              "vs_market_pct", "trend_30d", "unrealized_pnl", "unrealized_pnl_pct")
    assert [d.model_dump(include=set(fields)) for d in through] == \
           [d.model_dump(include=set(fields)) for d in direct]
    # The MCP run continues the wrapped source's price series, and is its own ledger run.
    assert json.loads((data_dir / "mcp.json").read_text())["source"] == "mock"
    assert all(len(get_history(d.card_id, source="mock")) == 1 for d in through)
    assert [r["source"] for r in ledger.recent_runs()] == ["mcp", "mock"]


def test_server_down_is_a_transient_error(data_dir, monkeypatch):
    from src.market import mcp_source
    from src.market.base import TransientSourceError

    def broken(self, upstream):
        raise OSError("server failed to start")
    monkeypatch.setattr(mcp_source._Session, "__init__", broken)
    with pytest.raises(TransientSourceError, match="MCP server unavailable"):
        mcp_source.McpSource(upstream="mock").fetch_market_data("Pikachu 58/102", "BS", "Raw")
