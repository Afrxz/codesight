"""Small client for the pokemontcg.io v2 API (free; key optional).

Used for two things: card art (src/images.py) and free TCGplayer prices for
raw cards (src/market/pokemontcg_source.py). Both need the same lookup —
"which printing is 'Mew ex 232/091'?" — so it lives here once.

Matching: search by name and collector number, then require the set's printed
total to equal the number after the slash, so "Charizard ex 199/165" matches
the 151 printing and not another set's #199.

Resilience: responses are cached in data/cache/pokemontcg/ (forever for art,
`ttl` for prices), and the API's occasional 5xx errors are retried with backoff.
An optional POKEMONTCG_API_KEY (free from dev.pokemontcg.io) raises rate limits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import requests

from . import config
from .cardnames import base_name, number_parts

log = logging.getLogger(__name__)

API_URL = "https://api.pokemontcg.io/v2/cards"
# Everything we ever need about one card. Lookups by id all use this selection,
# so a card record cached by one feature is reused by the others.
CARD_FIELDS = "id,name,number,set,rarity,images,tcgplayer"


def _cache_file(query: str, select: str, extra: dict):
    key = hashlib.sha256(f"{query}|{select}|{json.dumps(extra)}".encode()).hexdigest()[:24]
    return config.DATA_DIR / "cache" / "pokemontcg" / f"{key}.json"


def store(query: str, select: str, cards: list[dict], extra_params: dict | None = None) -> None:
    """Write a response into the cache as if it had just been fetched."""
    path = _cache_file(query, select, dict(sorted((extra_params or {}).items())))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "cards": cards}),
                    encoding="utf-8")


def remember_cards(cards: list[dict]) -> None:
    """Cache each card from a search under its own `id:` lookup.

    The search already returned the full record, so adding one of these cards
    (or its first price check) needs no second call to a sometimes-flaky API.
    """
    for card in cards:
        if card.get("id"):
            store(f"id:{card['id']}", CARD_FIELDS, [card])


def search(
    query: str,
    select: str,
    ttl: timedelta | None = None,
    session=None,
    sleep: Callable[[float], None] = time.sleep,
    extra_params: dict | None = None,
    attempts: int = 3,
    backoff_s: float = 2.0,
) -> list[dict] | None:
    """Run a card search. Returns the result list, or None if the API failed.

    `ttl=None` caches forever; otherwise a cached response older than `ttl` is
    refetched. An empty list is a real answer ("no such card") and is cached.
    `extra_params` (e.g. pageSize, orderBy) are part of the cache key. The
    interactive search uses fewer, shorter retries than background runs.
    """
    extra = dict(sorted((extra_params or {}).items()))
    cache_file = _cache_file(query, select, extra)
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(cached, list):          # older cache format: no timestamp
            cached = {"fetched_at": "1970-01-01T00:00:00+00:00", "cards": cached}
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])
        if ttl is None or age < ttl:
            return cached["cards"]

    session = session or requests.Session()
    headers = {}
    api_key = os.getenv("POKEMONTCG_API_KEY", "").strip()
    if api_key:
        headers["X-Api-Key"] = api_key

    for attempt in range(attempts):
        try:
            resp = session.get(API_URL, params={"q": query, "select": select, **extra},
                               headers=headers, timeout=60)
        except requests.RequestException as exc:
            log.warning("pokemontcg.io network error: %s", exc)
        else:
            if resp.ok:
                cards = resp.json().get("data", [])
                store(query, select, cards, extra)
                return cards
            log.warning("pokemontcg.io -> HTTP %d", resp.status_code)
            if resp.status_code < 500 and resp.status_code != 429:
                return None                   # bad request / bad key: retrying won't help
        if attempt < attempts - 1:
            sleep(backoff_s * 2 ** attempt)   # e.g. 2s, 4s
    return None


def find_card(name: str, select: str, ttl: timedelta | None = None, session=None,
              sleep: Callable[[float], None] = time.sleep) -> dict | None:
    """The pokemontcg.io record for a name like 'Mew ex 232/091', or None."""
    parts = number_parts(name)
    if parts is None:
        return None   # without a collector number we can't pick the right printing
    number, printed_total = parts
    # `set` is always selected: it's needed for the printed-total check.
    fields = ",".join(dict.fromkeys(["number", "set", *select.split(",")]))
    query = f'name:"{base_name(name)}" number:{number}'
    for card in search(query, fields, ttl, session, sleep) or []:
        if card.get("set", {}).get("printedTotal") == printed_total:
            return card
    return None


def check_key(api_key: str, session=None) -> tuple[bool, str]:
    """For the Settings panel's Test button: one uncached call with this key."""
    session = session or requests.Session()
    try:
        resp = session.get(API_URL, params={"q": "name:pikachu", "pageSize": 1},
                           headers={"X-Api-Key": api_key}, timeout=60)
    except requests.RequestException:
        return False, "Couldn't reach pokemontcg.io. Check your internet connection."
    if resp.status_code in (401, 403):
        return False, "pokemontcg.io rejected this key."
    if not resp.ok:
        return False, f"pokemontcg.io answered HTTP {resp.status_code}. It's sometimes flaky; try again."
    return True, "Key works. Higher rate limits are active."
