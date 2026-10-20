"""Live market source backed by tcgapi.net.

What the API offers (per tcgapi.net, checked 2026-10):
- Base URL `https://api.tcgapi.net`, auth via `Authorization: Bearer <key>`.
- `GET /v1/cards?q=...`  card search; each card carries `market_value`
  (`tcgplayer`, `pricecharting`) and `recent_sales[]` (`source`, `price`, `date`).
- `GET /v1/comps?q=...`  eBay sold comps. NOTE: listed under the Pro plan; on
  the free plan we expect 401/403 and fall back to `/v1/cards` recent_sales.
- Free plan: 1,000 requests/day. No documented listings endpoint and no
  documented grade fields — so grade matching is done here, from comp titles.

The three hard problems this module solves, in order:

1. **Card matching.** A text search returns many cards ("charizard" matches
   dozens). `_pick_card` scores candidates on name tokens, the collector number
   (e.g. "199/165") and the set, and takes the best one.
2. **Grade matching.** Raw and graded copies are different markets — a PSA 10
   can sell for 10x a raw copy. `matches_grade` parses each comp's grade (an
   explicit `grade` field, else the title, e.g. "... PSA 10 Gem Mint") and keeps
   only exact matches: same company AND same number. Raw targets reject anything
   that looks slabbed. Comps with no grade info at all are only trusted for raw
   targets (card-level `recent_sales` are raw market sales).
3. **Not hammering the API.** Every response is cached to `data/cache/tcgapi/`
   (24h TTL), live calls are spaced by a minimum interval, and 429/5xx responses
   are retried with exponential backoff (honouring `Retry-After`).

Everything is mapped onto the same `MarketData` contract the mock returns; on
any failure the source degrades to an empty `MarketData` instead of raising.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .. import config
from ..cardnames import base_name, card_number
from .base import MarketSource
from .types import Listing, MarketData, SoldComp

log = logging.getLogger(__name__)

BASE_URL = "https://api.tcgapi.net"
SOLD_WINDOW_DAYS = 30     # comps older than this don't count toward sold_avg
MAX_COMPS = 10            # sold_avg is the mean of the most recent N matching comps
SAMPLE_COMPS = 5          # how many comps to carry in MarketData for display

# Words that mark a listing as not-a-single-real-card. Kept short and obvious.
JUNK_WORDS = re.compile(r"\b(lot|bundle|proxy|custom|orica|replica|digital|fake)\b", re.I)
# "PSA 10", "BGS 9.5", "CGC10", "sgc 9" ...
GRADE_RE = re.compile(r"\b(PSA|BGS|CGC|SGC|TAG|ACE)\s*-?\s*(10|[1-9](?:\.5)?)\b", re.I)


# --- Grade parsing & matching (pure functions — the interview-critical bit) -- #

def parse_grade(text: str | None) -> tuple[str, str] | None:
    """Return ("PSA", "10") style tuple for a graded string, None if no grade found.

    >>> parse_grade("Charizard ex 199/165 PSA 10 Gem Mint")
    ('PSA', '10')
    >>> parse_grade("Raw NM") is None
    True
    """
    m = GRADE_RE.search(text or "")
    return (m.group(1).upper(), m.group(2)) if m else None


def is_raw(grade: str) -> bool:
    return parse_grade(grade) is None


def matches_grade(target_grade: str, comp_grade: str | None, comp_title: str | None) -> bool:
    """Does a sold comp belong to the same market as `target_grade`?

    - The comp's grade comes from its explicit grade field if present, else its title.
    - Graded target (e.g. "PSA 10"): the comp must parse to the SAME company and
      number. PSA 10 != PSA 9 != BGS 10. No grade info -> can't verify -> reject.
    - Raw target: reject anything that parses as graded; accept the rest.
    """
    target = parse_grade(target_grade)
    found = parse_grade(comp_grade) or parse_grade(comp_title)
    if target is None:           # raw target
        return found is None
    return found == target


# --- Tolerant field extraction ------------------------------------------------ #
# The comps response shape isn't documented, so read fields by a short list of
# likely names instead of hard-coding one.

def _records(payload: Any, keys=("data", "results", "comps", "sales", "items", "cards")) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in keys:
            if isinstance(payload.get(k), list):
                return [r for r in payload[k] if isinstance(r, dict)]
    return []


def _first(rec: Mapping, *keys: str) -> Any:
    for k in keys:
        if rec.get(k) not in (None, ""):
            return rec[k]
    return None


def _price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.]", "", value)   # "$1,234.50" -> "1234.50"
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None


def _date(value: Any) -> date | None:
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def check_key(api_key: str, session=None) -> tuple[bool, str]:
    """One-off check of a key, for the Settings panel's Test button.

    Makes at most two uncached requests: card search (free plan), then sold
    comps (Pro plan), so the message can say which features the key unlocks.
    """
    session = session or requests.Session()
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    def status(path: str) -> int | None:
        try:
            return session.get(BASE_URL + path, params={"q": "pikachu"},
                               headers=headers, timeout=15).status_code
        except requests.RequestException:
            return None

    cards = status("/v1/cards")
    if cards is None:
        return False, "Couldn't reach tcgapi.net. Check your internet connection."
    if cards in (401, 403):
        return False, "tcgapi.net rejected this key."
    if cards == 429:
        return False, "tcgapi.net says the rate limit is used up. Try again later."
    if cards != 200:
        return False, f"tcgapi.net answered HTTP {cards}."
    comps = status("/v1/comps")
    if comps == 200:
        return True, "Key works, including eBay sold comps (/v1/comps)."
    return True, ("Key works for card search. Sold comps (/v1/comps) aren't on this "
                  "plan, so graded cards won't get sold prices.")


# --- The source ----------------------------------------------------------------- #

class TcgApiSource(MarketSource):
    """`MarketSource` backed by tcgapi.net, with caching, pacing and retries."""

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        cache_ttl: timedelta | None = None,
        min_interval_s: float | None = None,
        max_retries: int = 3,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
    ):
        # Fails loudly with a readable ConfigError if TCGAPI_KEY is missing.
        self.api_key = api_key or config.get_tcgapi_key()
        self.cache_dir = cache_dir or config.DATA_DIR / "cache" / "tcgapi"
        # Defaults come from Settings ("Live data"); explicit args win (tests).
        self.cache_ttl = cache_ttl if cache_ttl is not None else timedelta(hours=config.get("CACHE_TTL_HOURS"))
        self.min_interval_s = min_interval_s if min_interval_s is not None else config.get("REQUEST_INTERVAL_S")
        self.max_retries = max_retries
        # session / sleep / today are injectable so tests run offline and instantly.
        self.session = session or requests.Session()
        self.sleep = sleep
        self.today = today
        self._last_call = 0.0
        self.live_calls = 0   # handy for asserting the cache works

    # -- HTTP with cache, pacing and backoff --------------------------------- #

    def _cache_path(self, path: str, params: Mapping) -> Path:
        key = hashlib.sha256(json.dumps([path, sorted(params.items())]).encode()).hexdigest()[:24]
        return self.cache_dir / f"{key}.json"

    def _get(self, path: str, params: Mapping) -> Any | None:
        """GET a JSON endpoint. Returns parsed JSON, or None on any failure."""
        cache_file = self._cache_path(path, params)
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])
            if age < self.cache_ttl:
                return cached["body"]

        for attempt in range(self.max_retries):
            # Pace live calls so a loop over many cards stays within rate limits.
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
            self._last_call = time.monotonic()
            self.live_calls += 1

            try:
                resp = self.session.get(
                    BASE_URL + path, params=params, timeout=15,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Accept": "application/json"},
                )
            except requests.RequestException as exc:
                log.warning("tcgapi %s network error (attempt %d): %s", path, attempt + 1, exc)
                self.sleep(2 ** attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                log.warning("tcgapi %s -> %d, retrying in %.0fs", path, resp.status_code, delay)
                self.sleep(delay)
                continue
            if resp.status_code in (401, 403):
                log.warning("tcgapi %s -> %d (key invalid, or endpoint not on your plan)",
                            path, resp.status_code)
                return None
            if resp.status_code == 404:
                body: Any = None          # "not found" is a real answer: cache it
            elif resp.ok:
                try:
                    body = resp.json()
                except ValueError:
                    log.warning("tcgapi %s returned non-JSON", path)
                    return None
            else:
                log.warning("tcgapi %s -> HTTP %d", path, resp.status_code)
                return None

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "path": path, "params": dict(params), "body": body,
            }), encoding="utf-8")
            return body

        log.warning("tcgapi %s gave up after %d attempts", path, self.max_retries)
        return None

    # -- Matching & normalization -------------------------------------------- #

    @staticmethod
    def _pick_card(cards: list[dict], name: str, set_code: str) -> dict | None:
        """Choose the search result that best matches our card, or None."""
        want_tokens = set(base_name(name).lower().split())
        want_number = card_number(name)
        best, best_score = None, 0
        for c in cards:
            blob = json.dumps(c).lower()   # name/number/set may live in any field
            cname = str(c.get("name", "")).lower()
            if not want_tokens <= set(re.findall(r"[\w']+", cname)):
                continue                   # every word of our name must appear
            score = 1
            if want_number and (want_number in blob or want_number.split("/")[0] in cname):
                score += 2                 # collector number is the strongest signal
            if set_code and set_code.lower() in blob:
                score += 1
            if score > best_score:
                best, best_score = c, score
        # With a collector number in our name, demand a number match — otherwise
        # "Charizard ex" could silently match a different Charizard ex printing.
        if want_number and best_score < 3:
            return None
        return best

    def _normalize_comps(self, records: list[dict], name: str, grade: str) -> list[SoldComp]:
        number = card_number(name)
        cutoff = self.today() - timedelta(days=SOLD_WINDOW_DAYS)
        comps = []
        for r in records:
            price = _price(_first(r, "price", "sold_price", "sale_price", "amount"))
            when = _date(_first(r, "date", "sold_date", "sold_at", "end_date", "ended_at"))
            title = _first(r, "title", "listing_title", "name")
            if price is None or when is None or when < cutoff:
                continue
            if title and JUNK_WORDS.search(title):
                continue
            # If the title names a collector number, it must be ours.
            if title and number and re.search(r"\b\d{1,3}/\d{1,3}\b", title) and number not in title:
                continue
            if not matches_grade(grade, _first(r, "grade", "condition"), title):
                continue
            comps.append(SoldComp(price=price, date=when))
        return sorted(comps, key=lambda c: c.date, reverse=True)

    def fetch_market_data(self, name: str, set_code: str, grade: str,
                          variant: str | None = None, catalog_id: str | None = None,
                          condition: str | None = None) -> MarketData:
        # `variant` / `catalog_id` are ignored: tcgapi.net comps don't report
        # finishes, and it has its own card ids.
        try:
            return self._fetch(name, set_code, grade)
        except Exception:
            # Contract: never raise. Log with traceback and return "no data".
            log.exception("tcgapi fetch failed for %s (%s)", name, grade)
            return MarketData()

    def _fetch(self, name: str, set_code: str, grade: str) -> MarketData:
        # 1) Card-level data (free plan): identity, market value, raw recent sales.
        card = self._pick_card(_records(self._get("/v1/cards", {"q": base_name(name)})),
                               name, set_code)

        # 2) Sold comps (Pro plan). Query includes the grade to narrow eBay results.
        query = f"{name} {grade}" if not is_raw(grade) else name
        comp_records = _records(self._get("/v1/comps", {"q": query}))
        if not comp_records and card is not None:
            # Fallback: the card's own recent_sales. These have no titles/grades,
            # so matches_grade only accepts them for raw targets.
            comp_records = _records(card.get("recent_sales"), keys=())

        comps = self._normalize_comps(comp_records, name, grade)[:MAX_COMPS]

        # 3) Listings. No listings endpoint is documented; use one if a card
        # payload carries listings, else (raw only) use the TCGplayer market
        # price as a proxy "best listing". For graded cards a raw market price
        # would be wrong, so leave it empty rather than mislead.
        listings: list[Listing] = []
        if card is not None:
            for rec in _records(card.get("listings") or card.get("ebay_listings"), keys=()):
                price = _price(_first(rec, "price", "amount"))
                cond = str(_first(rec, "condition", "grade", "title") or "")
                if price is not None and matches_grade(grade, cond, cond):
                    listings.append(Listing(price=price, condition=cond or grade,
                                            url=str(_first(rec, "url", "link") or "")))
            if not listings and is_raw(grade):
                mv = _price((card.get("market_value") or {}).get("tcgplayer"))
                if mv is not None:
                    listings.append(Listing(price=mv, condition="Raw (TCGplayer market)"))

        return MarketData(
            best_listing=min((x.price for x in listings), default=None),
            listings=sorted(listings, key=lambda x: x.price)[:5],
            sold_comps=comps[:SAMPLE_COMPS],
            sold_avg=round(sum(c.price for c in comps) / len(comps), 2) if comps else None,
            sold_count=len(comps),
            source_note=None if comps else (
                "tcgapi.net found no matching sales in the last 30 days"
                + ("." if card is not None else " (and didn't recognise the card).")),
        )
