"""Local web server for the binder UI — Python standard library only.

    python -m src.web              # http://127.0.0.1:8000
    python -m src.web --port 8080 --open

Routes:
    GET    /                    the binder page (web/index.html) and its CSS/JS
    GET    /api/binder          everything the page needs, as one JSON document
    POST   /api/run             run the agent once (?source=mock|real, else the saved setting)
    GET    /api/search?q=...    search the pokemontcg.io catalog ("charizard ex", "pikachu 58")
    POST   /api/cards           add a card from a search result (and give it a first recommendation)
    POST   /api/cards/<id>/price  set or clear your own price for a card
    PATCH  /api/cards/<id>      edit a card (status, condition, finish, what you paid, targets)
    DELETE /api/cards/<id>      remove a card (and its history), then return /api/binder
    GET    /api/track-record    how the agent's past calls turned out (7 and 30 days later)
    POST   /api/alerts/read     mark all alerts as read
    GET    /api/settings        the settings, with API keys masked (never sent back)
    POST   /api/settings        save {"updates": {...}} to .env
    POST   /api/settings/test   check an LLM or tcgapi key with one live call

Why no web framework: a handful of JSON routes is well within what the
standard library handles, with nothing new to install or explain. The server
binds to 127.0.0.1 only, serves a fixed whitelist of files (no path handling,
so no path traversal), requires a CSRF header on every write, and is
read-only towards every marketplace — its actions only touch local files.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

from . import alerts, catalog, config, evaluate, ledger, scheduler
from .db import init_db
from .manage import list_cards, remove_card, set_manual_price
from .memory import get_history
from .pnl import portfolio_summary
from .run import SOURCE_ALIASES, SOURCES, refresh_card, run, setup_logging

log = logging.getLogger("tcg.web")

WEB_DIR = config.PROJECT_ROOT / "web"

# Exact URL -> (file, content type). Anything else is a 404.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/binder.css": ("binder.css", "text/css; charset=utf-8"),
    "/binder.js": ("binder.js", "text/javascript; charset=utf-8"),
}

# Only one agent run at a time (a double-click shouldn't start two).
_run_lock = threading.Lock()

CARD_PATH = re.compile(r"^/api/cards/(\d+)$")
CARD_PRICE_PATH = re.compile(r"^/api/cards/(\d+)/price$")
CARD_HISTORY_PATH = re.compile(r"^/api/cards/(\d+)/price-history$")
RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}


def price_history(card_id: int, range_: str) -> dict | None:
    """Chart data for one card: JustTCG's own history when that's the price
    source (full history from day one), otherwise our daily snapshots."""
    card = next((c for c in list_cards() if c["id"] == card_id), None)
    if card is None:
        return None
    range_ = range_ if range_ in RANGE_DAYS else "30d"
    path = config.DATA_DIR / "decisions.json"
    latest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    source = latest.get("source") or config.get("RUN_SOURCE")

    if source == "justtcg" and os.getenv("JUSTTCG_API_KEY", "").strip() and card["grade"] == "Raw":
        from .market.justtcg import JustTcgSource
        result = JustTcgSource().price_history(card["name"], card["catalog_id"], card["variant"],
                                               card["condition"], range_)
        if result["points"]:
            return {**result, "source": "justtcg"}

    rows = get_history(card_id, days=RANGE_DAYS[range_], source=source)
    return {"points": [{"t": h["run_at"], "p": h["sold_avg"], "b": h["best_listing"]}
                       for h in rows if h["sold_avg"] is not None],
            "label": "Our daily snapshots", "range": range_, "source": "ours"}


def _refresh(card_id: int) -> bool:
    """Re-decide one card now. Shares the run lock, so it never interleaves
    with a full run writing decisions.json. Returns whether it refreshed."""
    if not _run_lock.acquire(timeout=60):
        return False
    try:
        return refresh_card(card_id) is not None
    finally:
        _run_lock.release()
MAX_BODY_BYTES = 16 * 1024


def check_connection(target: str, overrides: dict) -> tuple[bool, str]:
    """Back the Settings panel's Test buttons: one live call, with friendly errors.

    `overrides` are values typed into the form but not saved yet, so you can
    test a key before saving it. Anything not overridden uses the saved value.
    """
    def pick(key: str) -> str:
        raw = str(overrides.get(key) or "").strip()
        return config.validate(key, raw) if raw else (os.getenv(key, "").strip() or config.get(key) or "")

    try:
        if target == "webhook":
            url = pick("ALERT_WEBHOOK_URL")
            if not url:
                return False, "Enter a webhook URL first."
            try:
                resp = requests.post(url, timeout=15, json={
                    "content": "Card Binder: test alert. Alerts will arrive here.",
                    "text": "Card Binder: test alert. Alerts will arrive here."})
            except requests.RequestException:
                return False, "Couldn't reach that webhook URL."
            return (True, "Sent. Check your channel.") if resp.ok else \
                (False, f"The webhook answered HTTP {resp.status_code}.")

        if target == "justtcg":
            key = pick("JUSTTCG_API_KEY")
            if not key:
                return False, "Enter a JustTCG key first."
            from .market.justtcg import check_key as check_justtcg_key
            return check_justtcg_key(key)

        if target == "pokemontcg":
            key = pick("POKEMONTCG_API_KEY")
            if not key:
                return False, "Enter a pokemontcg.io key first (it's optional: prices work without one)."
            from .pokemontcg import check_key as check_pokemontcg_key
            return check_pokemontcg_key(key)

        if target == "tcgapi":
            key = pick("TCGAPI_KEY")
            if not key:
                return False, "Enter a TCG API key first."
            from .market.tcgapi import check_key
            return check_key(key)

        if target == "llm":
            key = pick("LLM_API_KEY")
            if not key:
                return False, "Enter an LLM API key first."
            settings = config.Settings(llm_api_key=key, llm_base_url=pick("LLM_BASE_URL"),
                                       llm_model=pick("LLM_MODEL"))
            return _check_llm(settings)
    except config.ConfigError as exc:      # a typed value failed validation
        return False, str(exc)
    return False, "Unknown test target."


def _check_llm(settings: config.Settings) -> tuple[bool, str]:
    import openai
    from .decide import make_llm

    llm = make_llm(settings, timeout=20, max_retries=0)
    try:
        llm('Reply with exactly this JSON object: {"ok": true}', "ping")
    except openai.AuthenticationError:
        return False, "The provider rejected this API key."
    except openai.NotFoundError:
        return False, f"The provider doesn't recognise the model “{settings.llm_model}”."
    except openai.APIConnectionError:
        return False, f"Couldn't reach {settings.llm_base_url}. Check the URL and your connection."
    except openai.APIStatusError as exc:
        return False, f"The provider answered HTTP {exc.status_code}."
    except Exception as exc:   # anything else: say what happened, briefly
        return False, f"Test failed: {str(exc)[:200]}"
    return True, f"Connected. {settings.llm_model} replied."


def build_binder() -> dict:
    """Join cards + latest decisions + market evidence + history into one payload.

    Cards that were added after the last run still appear, with `decision`
    and `market` set to null, so a new card shows up in the binder immediately.
    """
    init_db()
    path = config.DATA_DIR / "decisions.json"
    latest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    decisions = {d["card_id"]: d for d in latest.get("decisions", [])}
    markets = latest.get("markets", {})

    cards = [
        {
            **dict(card),
            "decision": decisions.get(card["id"]),
            "market": markets.get(str(card["id"])),
            # History from the same source as the latest decisions, so the
            # chart never mixes sample and real prices.
            "history": get_history(card["id"], days=30, source=latest.get("source") or "mock"),
        }
        for card in list_cards()
    ]
    # Summarise only cards that still exist: decisions.json is a snapshot of
    # the last run, so it can still hold a card removed since then.
    live_decisions = [c["decision"] for c in cards if c["decision"]]
    return {
        "generated_at": latest.get("generated_at"),
        "source": latest.get("source"),
        "summary": portfolio_summary(live_decisions),
        "cards": cards,
        "agent": agent_status(latest),
    }


def agent_status(latest: dict) -> dict:
    """What the agent is up to: schedule, alerts, briefing, track record headline."""
    record = evaluate.track_record()
    return {
        "schedule": scheduler.status(),
        "unread_alerts": alerts.unread_count(),
        "alerts": alerts.recent(20),
        "briefing": latest.get("portfolio"),
        "track_record": {h: {k: v[k] for k in ("graded", "pending", "right", "wrong", "hit_rate")}
                         for h, v in record.items()},
        "tools": config.get("AGENT_TOOLS") == "on" and int(config.get("AGENT_TOOL_BUDGET")) > 0,
        "last_run": last_run_status(),
    }


def last_run_status() -> dict | None:
    """The newest ledger run: how far it got and which cards failed (and why)."""
    runs = ledger.recent_runs(1)
    if not runs:
        return None
    r = runs[0]
    return {k: r[k] for k in ("run_id", "status", "started_at", "finished_at", "cards_total",
                              "cards_done", "cards_failed", "started_by", "error", "failed_cards")}


class BinderHandler(BaseHTTPRequestHandler):
    server_version = "TCGBinder/1.0"

    _body_consumed = False

    def _drain_body(self) -> None:
        """Read any request body we haven't read yet.

        Answering early (e.g. a CSRF 403) without reading the body leaves unread
        bytes on the socket, and closing it then makes Windows reset the
        connection, so the client may see "connection aborted" instead of our
        response. Draining first makes early answers reliable.
        """
        if self._body_consumed:
            return
        self._body_consumed = True
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= MAX_BODY_BYTES:
            self.rfile.read(length)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self._drain_body()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")   # always show the latest run
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _read_json(self) -> dict | None:
        """Parse a small JSON object body; send a 400 and return None if invalid."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send_json({"error": "request body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        try:
            self._body_consumed = True
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, HTTPStatus.BAD_REQUEST)
            return None
        return body

    # http.server calls do_GET / do_POST / do_DELETE. Each one runs its real
    # handler inside _guarded, so a bug becomes a JSON 500 with a readable
    # message (and a traceback in data/run.log) instead of a dropped
    # connection, which the browser can only report as "Failed to fetch".
    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        self._guarded(self._handle_get)

    def do_POST(self) -> None:  # noqa: N802
        self._guarded(self._handle_post)

    def do_DELETE(self) -> None:  # noqa: N802
        self._guarded(self._handle_delete)

    def do_PATCH(self) -> None:  # noqa: N802
        self._guarded(self._handle_patch)

    def _handle_patch(self) -> None:
        """Edit card: PATCH /api/cards/<id> with the editable fields."""
        match = CARD_PATH.match(urlparse(self.path).path)
        if not match:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self._csrf_ok():
            return
        body = self._read_json()
        if body is None:
            return
        card_id = int(match.group(1))
        try:
            catalog.update_from_form(card_id, body)
        except catalog.CatalogError as exc:
            status = HTTPStatus.NOT_FOUND if "no card" in str(exc) else HTTPStatus.BAD_REQUEST
            self._send_json({"error": str(exc), "errors": exc.errors}, status)
            return
        log.info("Edited card %s", card_id)
        refreshed = _refresh(card_id)
        self._send_json({"refreshed": refreshed, "binder": build_binder()})

    def _guarded(self, handler) -> None:
        try:
            handler()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # The browser hung up first. Normal: the search box cancels an older
            # search when you keep typing. Nothing to answer, and not an error.
            log.debug("Client disconnected during %s %s", self.command, urlparse(self.path).path)
        except Exception:
            log.exception("Unhandled error in %s %s", self.command, urlparse(self.path).path)
            try:
                self._send_json({"error": "Something went wrong on the server. Details are in data/run.log."},
                                HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                pass   # the response was already partly sent; nothing more to do

    def _handle_get(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/binder":
            self._send_json(build_binder())
        elif path == "/api/settings":
            self._send_json({"settings": config.describe()})
        elif path == "/api/search":
            self._get_search(urlparse(self.path))
        elif path == "/api/track-record":
            self._send_json(evaluate.track_record())
        elif CARD_HISTORY_PATH.match(path):
            card_id = int(CARD_HISTORY_PATH.match(path).group(1))
            range_ = parse_qs(urlparse(self.path).query).get("range", ["30d"])[0]
            result = price_history(card_id, range_)
            self._send_json(result, HTTPStatus.OK if result is not None else HTTPStatus.NOT_FOUND)
        elif path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            self._send(HTTPStatus.OK, (WEB_DIR / filename).read_bytes(), content_type)
        else:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _csrf_ok(self) -> bool:
        """CSRF guard for every state-changing request.

        A custom header can't be sent cross-origin without a CORS preflight,
        which this server never approves — so other websites open in your
        browser can't trigger runs or delete cards on your behalf.
        """
        if self.headers.get("X-Binder") == "1":
            return True
        self._send_json({"error": "missing X-Binder header"}, HTTPStatus.FORBIDDEN)
        return False

    def _handle_delete(self) -> None:
        match = CARD_PATH.match(urlparse(self.path).path)
        if not match:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self._csrf_ok():
            return
        try:
            name, history_rows = remove_card(int(match.group(1)))
        except ValueError as exc:            # no such card
            self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        log.info("Removed card %s (%s) and %d history rows", match.group(1), name, history_rows)
        self._send_json(build_binder())

    def _handle_post(self) -> None:
        url = urlparse(self.path)
        handlers = {
            "/api/run": self._post_run,
            "/api/cards": self._post_card,
            "/api/settings": self._post_settings,
            "/api/settings/test": self._post_settings_test,
            "/api/alerts/read": self._post_alerts_read,
        }
        price_match = CARD_PRICE_PATH.match(url.path)
        if price_match:
            if self._csrf_ok():
                self._post_card_price(int(price_match.group(1)))
            return
        handler = handlers.get(url.path)
        if handler is None:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if self._csrf_ok():
            handler(url)

    def _get_search(self, url) -> None:
        text = parse_qs(url.query).get("q", [""])[0]
        try:
            results = catalog.search(text)
        except catalog.CatalogError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except catalog.CatalogUnavailable as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        self._send_json({"results": results})

    def _post_card(self, url) -> None:
        body = self._read_json()
        if body is None:
            return
        try:
            card_id = catalog.card_from_catalog(str(body.get("catalog_id") or ""), body)
        except catalog.CatalogError as exc:
            self._send_json({"error": str(exc), "errors": exc.errors}, HTTPStatus.BAD_REQUEST)
            return
        except catalog.CatalogUnavailable as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        log.info("Added card %s from catalog id %s", card_id, body.get("catalog_id"))
        refreshed = _refresh(card_id)   # its first recommendation, without a full run
        self._send_json({"card_id": card_id, "refreshed": refreshed, "binder": build_binder()},
                        HTTPStatus.CREATED)

    def _post_card_price(self, card_id: int) -> None:
        """Set or clear your own price: {"price": 12.5} or {"price": null}."""
        body = self._read_json()
        if body is None:
            return
        raw = body.get("price")
        try:
            price = None if raw in (None, "") else float(raw)
            set_manual_price(card_id, price)
        except (TypeError, ValueError) as exc:
            message = str(exc) if "no card" in str(exc) else "Enter a price from 0 to 10,000,000."
            status = HTTPStatus.NOT_FOUND if "no card" in str(exc) else HTTPStatus.BAD_REQUEST
            self._send_json({"error": message, "errors": {"price": message}}, status)
            return
        log.info("Card %s: own price %s", card_id, "cleared" if price is None else "set")
        refreshed = _refresh(card_id)
        self._send_json({"refreshed": refreshed, "binder": build_binder()})

    def _post_alerts_read(self, url) -> None:
        alerts.mark_all_read()
        self._send_json({"unread_alerts": 0})

    def _post_settings(self, url) -> None:
        body = self._read_json()
        if body is None:
            return
        updates = body.get("updates")
        if not isinstance(updates, dict):
            self._send_json({"error": "expected {\"updates\": {...}}"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            config.save(updates)
        except config.ConfigError as exc:
            self._send_json({"error": str(exc), "errors": exc.errors}, HTTPStatus.BAD_REQUEST)
            return
        # Log which settings changed, never their values (some are secrets).
        log.info("Settings updated: %s", ", ".join(sorted(updates)))
        self._send_json({"settings": config.describe()})

    def _post_settings_test(self, url) -> None:
        body = self._read_json()
        if body is None:
            return
        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict):
            overrides = {}
        ok, message = check_connection(str(body.get("target")), overrides)
        self._send_json({"ok": ok, "message": message})

    def _post_run(self, url) -> None:
        # No ?source= means "use the saved setting".
        source = parse_qs(url.query).get("source", [None])[0]
        if source not in (None, *SOURCES, *SOURCE_ALIASES):
            self._send_json({"error": f"source must be one of {', '.join(SOURCES)}"},
                            HTTPStatus.BAD_REQUEST)
            return
        if not _run_lock.acquire(blocking=False):
            self._send_json({"error": "a run is already in progress"}, HTTPStatus.CONFLICT)
            return
        try:
            run(source, started_by="binder")
        except config.ConfigError as exc:   # e.g. --source real without TCGAPI_KEY
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        finally:
            _run_lock.release()
        self._send_json(build_binder())

    def log_message(self, fmt: str, *args) -> None:
        # Route the default stderr access log through logging, at DEBUG.
        log.debug("%s - %s", self.address_string(), fmt % args)


def make_server(port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), BinderHandler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the binder UI locally.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="open it in your browser")
    args = parser.parse_args(argv)

    setup_logging()
    server = make_server(args.port)
    url = f"http://127.0.0.1:{args.port}"
    log.info("Binder running at %s (Ctrl+C to stop)", url)
    if args.open:
        webbrowser.open(url)
    # The agent runs itself daily while the binder is open (Settings -> Agent).
    daily = scheduler.DailyScheduler(run_once=lambda: run(started_by="schedule"), lock=_run_lock)
    daily.start()
    # Finish a run the last process didn't (crash, closed window) in the
    # background, so the binder opens straight away.
    threading.Thread(target=scheduler.resume_interrupted_run, args=(_run_lock,),
                     name="resume-run", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        daily.stop()
        server.server_close()


if __name__ == "__main__":
    main()
