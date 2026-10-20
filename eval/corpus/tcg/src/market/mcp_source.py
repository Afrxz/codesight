"""McpSource: the agent gets its market data through the project's MCP server (Phase 9.3).

A drop-in MarketSource, selected with `--source mcp` (or RUN_SOURCE=mcp). It
launches `mcp_server` as a child process over stdio, using the official MCP
client, and turns each tool result back into the same `MarketData` the direct
source would have returned. The decision loop doesn't know the difference.

The MCP server is just a transport in front of the same data, so:
- `prefetch` calls get_market_data_batch once, letting batch-capable sources
  (JustTCG: 20 cards per request) spend as little quota as they do directly;
- a `not_found` error becomes MarketData(source_note=...), exactly what the
  direct source returns for a card without a price;
- a broken connection (server crashed, timed out) raises TransientSourceError,
  so the run loop's retry kicks in, and the next call starts a fresh server.

Sync vs async. The MCP client is async (anyio), the agent's loop is plain
sync code. One background thread owns an event loop and the MCP session for
the life of the source. Sync calls hand it requests through a queue and wait
for the answer. The session must be opened and closed by the same task, which
is why a single long-lived task owns it instead of each call opening its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from concurrent.futures import Future
from datetime import timedelta

from .. import config
from .base import MarketSource, TransientSourceError
from .types import MarketData

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 120     # one tool call (a batch on a cold JustTCG cache can take a while)
START_TIMEOUT_S = 60     # launching the server process


def _card_key(name, set_code, grade, variant, catalog_id, condition) -> tuple:
    return (name, set_code or "", grade or "Raw", variant, catalog_id, condition)


class _Session:
    """A long-lived MCP client session on a background thread, callable from sync code."""

    def __init__(self, upstream: str):
        from mcp import StdioServerParameters
        env = dict(os.environ, MCP_SOURCE=upstream, TCG_DATA_DIR=str(config.DATA_DIR),
                   PYTHONIOENCODING="utf-8")
        self.params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"],
                                            cwd=str(config.PROJECT_ROOT), env=env)
        self.loop = asyncio.new_event_loop()
        self.ready: Future = Future()
        self.closed: Future = Future()
        self.thread = threading.Thread(target=self._thread_main, name="mcp-client", daemon=True)
        self.thread.start()
        self.ready.result(timeout=START_TIMEOUT_S)          # raises if the server didn't start

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._owner())
        except BaseException as exc:                         # surfaced through the futures
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            if not self.closed.done():
                self.closed.set_result(None)
            self.loop.close()

    async def _owner(self) -> None:
        """Open the session, serve queued calls one at a time, close it on request."""
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        self.queue: asyncio.Queue = asyncio.Queue()
        async with stdio_client(self.params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            self.ready.set_result(True)
            while True:
                item = await self.queue.get()
                if item is None:                             # close()
                    return
                name, args, future = item
                try:
                    result = await session.call_tool(name, args,
                                                     read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_S))
                    future.set_result(result)
                except Exception as exc:
                    future.set_exception(exc)

    @property
    def alive(self) -> bool:
        return self.thread.is_alive() and not self.closed.done()

    def call(self, name: str, args: dict):
        future: Future = Future()
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (name, args, future))
        return future.result(timeout=CALL_TIMEOUT_S + 5)

    def close(self) -> None:
        if self.alive:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
            self.thread.join(timeout=10)


class McpSource(MarketSource):
    """MarketSource backed by the MCP server (which wraps the `upstream` source)."""

    def __init__(self, upstream: str | None = None):
        self.upstream = upstream or config.get("MCP_SOURCE")   # the price series this really is
        self._session: _Session | None = None
        self._prefetched: dict[tuple, MarketData] = {}

    # -- transport --------------------------------------------------------------- #

    def _call(self, name: str, args: dict):
        """Call one tool; transport failures become TransientSourceError (retried by the run)."""
        try:
            if self._session is None or not self._session.alive:
                self._session = _Session(self.upstream)
            return self._session.call(name, args)
        except Exception as exc:
            if self._session is not None:
                self._session.close()
            self._session = None                              # next call starts a fresh server
            raise TransientSourceError(f"MCP server unavailable: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _error(result) -> dict:
        """The {"code", "message"} payload of an isError result (the SDK may prefix text)."""
        text = "".join(getattr(c, "text", "") for c in result.content)
        try:
            return json.loads(text[text.index("{"):])
        except ValueError:
            return {"code": "internal", "message": text or "unknown MCP server error"}

    def _to_market(self, error: dict) -> MarketData:
        if error.get("code") == "not_found":
            return MarketData(source_note=error.get("message"))   # same as the direct source
        if error.get("code") == "invalid_argument":
            raise ValueError(f"MCP server rejected the request: {error.get('message')}")
        raise RuntimeError(f"MCP server error: {error.get('message')}")

    # -- MarketSource ------------------------------------------------------------- #

    def prefetch(self, cards) -> None:
        """One get_market_data_batch call for the whole run (see the module docstring)."""
        queries = [{"name": c["name"], "set_code": c["set_code"] or "", "grade": c["grade"] or "Raw",
                    "variant": c["variant"], "catalog_id": c["catalog_id"], "condition": c["condition"]}
                   for c in (dict(card) for card in cards)]
        if not queries:
            return
        result = self._call("get_market_data_batch", {"cards": queries})
        if result.isError:
            log.warning("MCP batch failed (%s); falling back to per-card calls", self._error(result))
            return
        for item in result.structuredContent["results"]:
            q = item["query"]
            key = _card_key(q["name"], q["set_code"], q["grade"], q["variant"], q["catalog_id"], q["condition"])
            if item.get("market") is not None:
                self._prefetched[key] = MarketData.model_validate(item["market"])
            elif (item.get("error") or {}).get("code") == "not_found":
                self._prefetched[key] = MarketData(source_note=item["error"]["message"])
            # invalid_argument: left out, so the per-card call raises it clearly

    def fetch_market_data(self, name: str, set_code: str, grade: str, variant: str | None = None,
                          catalog_id: str | None = None, condition: str | None = None) -> MarketData:
        key = _card_key(name, set_code, grade, variant, catalog_id, condition)
        if key in self._prefetched:
            return self._prefetched.pop(key)
        result = self._call("get_market_data", {"name": name, "set_code": set_code or "",
                                                "grade": grade or "Raw", "variant": variant,
                                                "catalog_id": catalog_id, "condition": condition})
        if result.isError:
            return self._to_market(self._error(result))
        return MarketData.model_validate(result.structuredContent)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
