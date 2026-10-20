"""JustTCG (justtcg.com): condition- and printing-level prices, including new sets.

Checked against live responses (2026-10). A card looks like:

    {"id": "pokemon-me-30th-celebration-zapdos-133-128-illustration-rare",
     "name": "Zapdos - 133/128", "number": "133/128",
     "set": "me-30th-celebration-pokemon", "set_name": "ME: 30th Celebration",
     "variants": [{"condition": "Near Mint", "printing": "Holofoil", "price": 5.17,
                   "priceStatus": "current", "lastUpdated": 1792434686,
                   "priceChange30d": -65.96, ...}]}

How it maps onto MarketData:
- sold_avg      <- the Near Mint price for the card's printing ("the last price
                   the market reported"). Raw cards only: no graded prices.
- best_listing  <- None: JustTCG reports market prices, not listings, so the
                   "% under market" buy signal can't fire from this source.
- sold_count    <- None: no sales count, so confidence tops out at medium.
- trend_30d_pct <- priceChange30d, so cards have a trend from the first run.

Fitting the free plan (1,000 requests/month, 100/day, 10/minute):
1. Each card's JustTCG id is found once (a set lookup + a search) and cached
   for good in data/cache/justtcg/ids.json.
2. Prices are then fetched in batches of up to 20 cards per request
   (POST /cards) via `prefetch`, and cached for CACHE_TTL_HOURS.
So a 20-card collection costs about one request a day.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .. import config
from ..cardnames import base_name, number_parts
from ..pokemontcg import CARD_FIELDS
from ..pokemontcg import search as pokemontcg_search
from .base import MarketSource
from .tcgapi import is_raw
from .types import MarketData

log = logging.getLogger(__name__)

BASE_URL = "https://api.justtcg.com/v1"
BATCH_SIZE = 20                 # free plan's cards-per-request limit
MIN_INTERVAL_S = 6.5            # free plan: 10 requests/minute
CONDITION = "Near Mint"         # raw cards are valued as Near Mint

# Our finish keys (pokemontcg.io's naming) -> JustTCG printing names.
# "Holofoil" and "Normal" are confirmed in live data; the rest follow the
# same TCGplayer naming and are compared case-insensitively.
PRINTINGS = {
    "holofoil": "Holofoil",
    "normal": "Normal",
    "reverseHolofoil": "Reverse Holofoil",
    "1stEditionHolofoil": "1st Edition Holofoil",
    "1stEditionNormal": "1st Edition",
    "unlimitedHolofoil": "Unlimited Holofoil",
    "unlimited": "Unlimited",
}


# --- Pure matching helpers (unit-tested) ------------------------------------ #

def normalize_set(name: str) -> str:
    """'ME: 30th Celebration Classic Collection' -> '30th celebration classic collection'.

    JustTCG prefixes set names with a series code ("ME:", "SV:", "SWSH07:");
    pokemontcg.io doesn't. Strip that and punctuation so the two line up.
    """
    name = re.sub(r"^[A-Za-z0-9]{1,8}:\s*", "", name or "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())


def pick_set(sets: Sequence[dict], wanted: str) -> dict | None:
    """Best JustTCG set for a pokemontcg.io set name: exact match first, then
    the shortest name containing all of the wanted words."""
    target = normalize_set(wanted)
    if not target:
        return None
    exact = [s for s in sets if normalize_set(s.get("name", "")) == target]
    if exact:
        return exact[0]
    words = set(target.split())
    containing = [s for s in sets if words <= set(normalize_set(s.get("name", "")).split())]
    return min(containing, key=lambda s: len(s.get("name", "")), default=None)


def same_number(theirs: str | None, ours: str) -> bool:
    """'133/128' vs '133', '055/128' vs '55', 'TG05/TG30' vs 'TG05'."""
    if not theirs:
        return False
    head = theirs.split("/")[0].strip()
    if head.isdigit() and ours.isdigit():
        return int(head) == int(ours)
    return head.lower() == ours.lower()


def _english(v: dict) -> bool:
    # The docs say `language` is only set for non-English printings, but live
    # responses include "English" explicitly, so accept both.
    return (v.get("language") or "English") == "English"


def _in_condition(v: dict, condition: str = CONDITION) -> bool:
    return v.get("condition") == condition and _english(v)


def choose_printing(variants: Sequence[dict], wanted: str | None,
                    condition: str = CONDITION) -> str | None:
    """Which printing to price, among English variants in `condition` that have one."""
    available = {v["printing"] for v in variants if _in_condition(v, condition) and v.get("printing")}
    if wanted:
        target = PRINTINGS.get(wanted, wanted).lower()
        return next((p for p in available if p.lower() == target), None)   # never swap finishes
    for preferred in PRINTINGS.values():
        if preferred in available:
            return preferred
    return next(iter(sorted(available)), None)


def product_url(card: dict) -> str | None:
    """TCGplayer product page for the card, to check the price yourself."""
    tid = card.get("tcgplayerId")
    return f"https://www.tcgplayer.com/product/{tid}" if tid else None


CONDITION_ORDER = ("Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged")


def by_condition(card: dict) -> list[dict]:
    """Every English condition/printing with a current price, best condition first."""
    order = {c: i for i, c in enumerate(CONDITION_ORDER)}
    rows = [{"condition": v.get("condition"), "printing": v.get("printing"),
             "price": v.get("price"), "change_30d": v.get("priceChange30d")}
            for v in card.get("variants") or []
            if _english(v) and v.get("price") is not None and v.get("condition") != "Sealed"]
    return sorted(rows, key=lambda r: (r["printing"] or "", order.get(r["condition"], 9)))


def to_market_data(card: dict, wanted_variant: str | None, condition: str | None = None) -> MarketData:
    """Map one JustTCG card record onto MarketData (see module docstring).

    Prices the copy's condition (Near Mint by default). If JustTCG has no price
    for that condition, it falls back to Near Mint and says so in the note.
    """
    variants = card.get("variants") or []
    wanted_condition = condition or CONDITION
    used_condition, fallback_note = wanted_condition, ""
    printing = choose_printing(variants, wanted_variant, wanted_condition)
    if printing is None and wanted_condition != CONDITION:
        printing = choose_printing(variants, wanted_variant, CONDITION)
        if printing is not None:
            used_condition = CONDITION
            fallback_note = (f" No {wanted_condition} price was reported, so this is the Near Mint "
                             f"price; your copy is likely worth less.")
    common = {"by_condition": by_condition(card), "product_url": product_url(card)}
    if printing is None:
        have = sorted({v.get("printing") for v in variants if v.get("condition") == CONDITION} - {None})
        if wanted_variant and have:
            note = (f"JustTCG has no Near Mint {PRINTINGS.get(wanted_variant, wanted_variant)} price "
                    f"for this card (it has: {', '.join(have)}).")
        else:
            note = "JustTCG has no Near Mint price for this card yet."
        return MarketData(source_note=note, **common)

    v = next(x for x in variants if _in_condition(x, used_condition) and x.get("printing") == printing)
    updated = (datetime.fromtimestamp(v["lastUpdated"], tz=timezone.utc).date().isoformat()
               if v.get("lastUpdated") else None)
    if v.get("priceStatus") != "current" or v.get("price") is None:
        last = v.get("lastKnownPrice")
        return MarketData(source_note=(
            f"JustTCG's {used_condition} {printing} price has expired"
            + (f" (last seen at ${last:,.2f} on {updated})" if last is not None and updated else "")
            + "; no current reports."), **common)

    change = v.get("priceChange30d")
    avg30 = v.get("avgPrice30d")
    stats = {k: v.get(src) for k, src in (
        ("change_7d_pct", "priceChange7d"), ("change_90d_pct", "priceChange90d"),
        ("low_30d", "minPrice30d"), ("high_30d", "maxPrice30d"),
        ("position_in_30d_range", "priceRelativeTo30dRange")) if v.get(src) is not None}
    return MarketData(
        best_listing=None,
        sold_avg=round(float(v["price"]), 2),
        sold_count=None,
        trend_30d_pct=change,
        avg_30d=round(float(avg30), 2) if avg30 else None,
        stats=stats,
        source_note=(f"JustTCG {used_condition} {printing} price"
                     + (f", updated {updated}" if updated else "")
                     + (f" ({change:+.1f}% over 30 days)" if change is not None else "")
                     + ". JustTCG reports market prices, not listings." + fallback_note),
        **common,
    )


# --- The source ---------------------------------------------------------------- #

class JustTcgSource(MarketSource):
    """`MarketSource` backed by JustTCG, with id caching, batching and pacing."""

    def __init__(self, api_key: str | None = None, cache_ttl: timedelta | None = None,
                 session=None, sleep: Callable[[float], None] = time.sleep,
                 min_interval_s: float | None = None):
        key = api_key or config.get_justtcg_key()   # fails loudly if missing
        self.headers = {"x-api-key": key}
        self.cache_ttl = cache_ttl if cache_ttl is not None else timedelta(hours=config.get("CACHE_TTL_HOURS"))
        self.session = session or requests.Session()
        self.sleep = sleep
        self.min_interval_s = MIN_INTERVAL_S if min_interval_s is None else min_interval_s
        self._last_call = 0.0
        self.live_calls = 0
        self.cache_dir = config.DATA_DIR / "cache" / "justtcg"
        self._prices: dict[str, dict] = {}    # JustTCG card id -> card record, from prefetch

    # -- HTTP ----------------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs):
        """One paced request with a retry on 429/5xx. Returns parsed JSON or None."""
        for attempt in range(3):
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0 and self.live_calls:
                self.sleep(wait)
            self._last_call = time.monotonic()
            self.live_calls += 1
            try:
                resp = self.session.request(method, BASE_URL + path, headers=self.headers,
                                            timeout=30, **kwargs)
            except requests.RequestException as exc:
                log.warning("JustTCG %s network error: %s", path, exc)
                self.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After", "")
                self.sleep(float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                log.warning("JustTCG rejected the API key (HTTP %d)", resp.status_code)
                return None
            if not resp.ok:
                log.warning("JustTCG %s -> HTTP %d", path, resp.status_code)
                return None
            body = resp.json()
            remaining = (body.get("_metadata") or {}).get("apiRequestsRemaining")
            if remaining is not None and remaining < 50:
                log.warning("JustTCG: only %s requests left on this plan", remaining)
            return body
        return None

    # -- caches ---------------------------------------------------------------- #

    def _read(self, name: str) -> dict:
        path = self.cache_dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _write(self, name: str, data: dict) -> None:
        path: Path = self.cache_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def _fresh(self, entry: dict | None) -> bool:
        if not entry:
            return False
        age = datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched_at"])
        return age < self.cache_ttl

    # -- step 1: find each card's JustTCG id (once, cached forever) ------------- #

    def _identity(self, name: str, catalog_id: str | None) -> tuple[str, str, str | None]:
        """(card name, collector number, set name) for matching, best from the catalog."""
        if catalog_id:
            cards = pokemontcg_search(f"id:{catalog_id}", CARD_FIELDS, ttl=None) or []
            if cards:
                c = cards[0]
                return c["name"], str(c["number"]), c.get("set", {}).get("name")
        parts = number_parts(name)
        if parts:
            return base_name(name), str(parts[0]), None
        # Names like "Charizard 4" or "Zeraora VSTAR GG01": the last word is the number.
        words = name.split()
        if len(words) > 1 and any(ch.isdigit() for ch in words[-1]):
            return " ".join(words[:-1]), words[-1], None
        return name, "", None

    def _set_id(self, set_name: str) -> str | None:
        """JustTCG's id for a pokemontcg.io set name; found once, then cached.

        JustTCG's set search doesn't match long names with punctuation
        ("30th Celebration: Classic Collection" finds nothing; "30th" finds
        both 30th sets), so it's queried with the first word and the best
        candidate is picked locally with pick_set. Misses aren't cached, so a
        set that JustTCG adds later is found on the next run.
        """
        sets_cache = self._read("sets.json")
        if sets_cache.get(set_name):
            return sets_cache[set_name]
        words = normalize_set(set_name).split()
        if not words:
            return None
        body = self._request("GET", "/sets", params={"game": "pokemon", "q": words[0]})
        match = pick_set((body or {}).get("data", []), set_name)
        if match is None:
            return None
        sets_cache[set_name] = match["id"]
        self._write("sets.json", sets_cache)
        return match["id"]

    def resolve(self, name: str, catalog_id: str | None) -> tuple[str | None, str | None]:
        """(JustTCG card id, or None + a note saying why)."""
        key = catalog_id or name
        ids = self._read("ids.json")
        if key in ids:
            return ids[key], None

        card_name, number, set_name = self._identity(name, catalog_id)
        params = {"game": "pokemon", "q": card_name, "limit": BATCH_SIZE}
        if set_name:
            set_id = self._set_id(set_name)
            if set_id is None:
                return None, f"Couldn't find the set “{set_name}” on JustTCG."
            params["set"] = set_id
        body = self._request("GET", "/cards", params=params)
        if body is None:
            return None, "JustTCG didn't respond (or rejected the key); the next run will try again."

        matches = [c for c in body.get("data", [])
                   if same_number(c.get("number"), number) and c.get("number") != "N/A"]
        if not matches:
            return None, "Couldn't find this card on JustTCG."
        if len({c.get("set") for c in matches}) > 1:
            return None, ("Several JustTCG sets have this card and number; add it through "
                          "+ Add card so its set is known.")
        ids[key] = matches[0]["id"]
        self._write("ids.json", ids)
        self._store_prices(matches[0])   # the search already returned its prices
        return matches[0]["id"], None

    # -- step 2: prices, in batches ----------------------------------------------- #

    def _store_prices(self, card: dict) -> None:
        prices = self._read("prices.json")
        prices[card["id"]] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "card": card}
        self._write("prices.json", prices)
        self._prices[card["id"]] = card

    def prefetch(self, cards: Sequence) -> None:
        """Resolve ids, then fetch every stale price in batches of 20."""
        prices = self._read("prices.json")
        stale: list[str] = []
        for card in cards:
            if not is_raw(card["grade"]):
                continue
            jt_id, _ = self.resolve(card["name"], card["catalog_id"])
            if jt_id is None:
                continue
            if self._fresh(prices.get(jt_id)):
                self._prices[jt_id] = prices[jt_id]["card"]
            elif jt_id not in self._prices:
                stale.append(jt_id)
        for i in range(0, len(stale), BATCH_SIZE):
            chunk = stale[i:i + BATCH_SIZE]
            # Every condition comes back (same request count), for the per-condition table.
            body = self._request("POST", "/cards", json=[{"cardId": c} for c in chunk])
            for card in (body or {}).get("data", []):
                self._store_prices(card)
        if stale:
            log.info("JustTCG: fetched %d prices in %d request(s)", len(stale),
                     -(-len(stale) // BATCH_SIZE))

    def fetch_market_data(self, name: str, set_code: str, grade: str,
                          variant: str | None = None, catalog_id: str | None = None,
                          condition: str | None = None) -> MarketData:
        try:
            if not is_raw(grade):
                return MarketData(source_note="JustTCG prices raw cards by condition, not graded slabs.")
            jt_id, why = self.resolve(name, catalog_id)
            if jt_id is None:
                return MarketData(source_note=why)
            card = self._prices.get(jt_id)
            if card is None:   # not prefetched (e.g. a single-card refresh)
                self.prefetch([{"name": name, "grade": grade, "catalog_id": catalog_id}])
                card = self._prices.get(jt_id)
            if card is None:
                return MarketData(source_note="JustTCG didn't return a price; the next run will try again.")
            return to_market_data(card, variant, condition)
        except Exception:
            log.exception("JustTCG fetch failed for %s", name)   # contract: never raise
            return MarketData(source_note="Something went wrong reading JustTCG's data; see data/run.log.")

    # -- price history for the binder's chart (on demand, cached 12h) ------------- #

    def price_history(self, name: str, catalog_id: str | None, variant: str | None,
                      condition: str | None, range_: str) -> dict:
        """{"points": [{"t", "p"}], "label", "range", "product_url"} for one card.

        One GET per card and range (priceHistoryDuration), cached for 12 hours,
        and only when someone opens the chart, so it barely touches the quota.
        """
        if range_ not in HISTORY_RANGES:
            range_ = "30d"
        jt_id, why = self.resolve(name, catalog_id)
        if jt_id is None:
            return {"points": [], "label": why or "Not found on JustTCG", "range": range_}
        cache = self._read("history.json")
        key = f"{jt_id}|{range_}"
        entry = cache.get(key)
        fresh = entry and datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched_at"]) < HISTORY_TTL
        if not fresh:
            body = self._request("GET", "/cards", params={"cardId": jt_id, "priceHistoryDuration": range_})
            records = (body or {}).get("data") or []
            if not records:
                return {"points": [], "label": "JustTCG didn't return a price history", "range": range_}
            entry = {"fetched_at": datetime.now(timezone.utc).isoformat(), "card": records[0]}
            cache[key] = entry
            self._write("history.json", cache)
        points, label = history_points(entry["card"], variant, condition)
        return {"points": points, "label": label, "range": range_, "product_url": product_url(entry["card"])}


HISTORY_RANGES = ("7d", "30d", "90d", "1y")
HISTORY_TTL = timedelta(hours=12)


def history_points(card: dict, wanted_variant: str | None, condition: str | None) -> tuple[list[dict], str]:
    """(points, label) for the copy's condition/printing from a JustTCG record."""
    variants = card.get("variants") or []
    used = condition or CONDITION
    printing = choose_printing(variants, wanted_variant, used)
    if printing is None and used != CONDITION:
        used, printing = CONDITION, choose_printing(variants, wanted_variant, CONDITION)
    if printing is None:
        return [], "JustTCG has no price history for this card"
    v = next(x for x in variants if _in_condition(x, used) and x.get("printing") == printing)
    points = [{"t": datetime.fromtimestamp(pt["t"], tz=timezone.utc).isoformat(timespec="seconds"),
               "p": pt["p"]} for pt in (v.get("priceHistory") or []) if pt.get("p") is not None]
    return sorted(points, key=lambda x: x["t"]), f"JustTCG · {used} {printing}"


def check_key(api_key: str, session=None) -> tuple[bool, str]:
    """For the Settings panel's Test button: one call to the cheap /games endpoint."""
    session = session or requests.Session()
    try:
        resp = session.get(f"{BASE_URL}/games", headers={"x-api-key": api_key}, timeout=20)
    except requests.RequestException:
        return False, "Couldn't reach JustTCG. Check your internet connection."
    if resp.status_code in (401, 403):
        return False, "JustTCG rejected this key."
    if resp.status_code == 429:
        return False, "JustTCG's rate limit is used up for now (the free plan allows 100 requests a day)."
    if not resp.ok:
        return False, f"JustTCG answered HTTP {resp.status_code}."
    try:
        meta = resp.json().get("_metadata") or {}
    except ValueError:
        meta = {}
    plan, remaining = meta.get("apiPlan"), meta.get("apiRequestsRemaining")
    detail = f" Plan: {plan}." if plan else ""
    if remaining is not None:
        detail += f" Requests left: {remaining}."
    return True, "Key works." + detail
