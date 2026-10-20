"""The card catalog behind the binder's "Add card" search.

Two jobs, both backed by pokemontcg.io:

1. `search(text)` turns what a person types ("charizard ex", "pikachu 58",
   "199/165") into a safe pokemontcg.io query and returns compact results,
   including each finish's current TCGplayer market price.
2. `card_from_catalog(id, ...)` builds a binder card from a catalog record.
   The server looks the card up again by id instead of trusting names sent by
   the browser, so the stored name / number / set / image always come from the
   catalog, in exactly the "Name NNN/TTT" format the price lookup matches on.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from . import config
from .db import VALID_STATUSES
from .manage import CONDITIONS, add_card, list_cards, update_card
from .market.pokemontcg_source import VARIANTS
from .pokemontcg import CARD_FIELDS, remember_cards
from .pokemontcg import search as api_search

SEARCH_FIELDS = CARD_FIELDS
PAGE_SIZE = 30
ID_RE = re.compile(r"^[A-Za-z0-9.\-]{2,40}$")     # e.g. sv4pt5-232, swsh12pt5gg-GG01
GRADE_RE = re.compile(r"^(Raw|(PSA|BGS|CGC|SGC|TAG|ACE) (10|[1-9](\.5)?))$")


class CatalogError(ValueError):
    """Bad input, with per-field messages in `errors` when there are several."""

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(message)
        self.errors = errors or {}


class CatalogUnavailable(RuntimeError):
    """pokemontcg.io didn't answer (it has bursts of 5xx errors)."""


# --- Searching -------------------------------------------------------------- #

def build_query(text: str) -> str:
    """Turn free text into a pokemontcg.io query.

    - Words become prefix matches on the name, all required:
      "char ex" -> name:char* name:ex*
    - A number ("58", "#58") filters the collector number; "199/165" also
      filters the set's printed total.
    - Anything that could change the query's meaning (quotes, colons,
      brackets, wildcards...) is stripped, so typed text can't inject syntax.
    """
    clauses = []
    for token in text.split()[:8]:
        number = re.fullmatch(r"#?(\d{1,3})(?:/(\d{1,3}))?", token)
        if number:
            clauses.append(f"number:{int(number.group(1))}")
            if number.group(2):
                clauses.append(f"set.printedTotal:{int(number.group(2))}")
            continue
        word = re.sub(r"[^\w'\-]", "", token)   # letters, digits, ' and - only
        if word:
            clauses.append(f"name:{word}*")
    if not clauses:
        raise CatalogError("Type part of a card name or number.")
    return " ".join(clauses)


def display_name(card: dict) -> str:
    """'Mew ex 232/091': name plus collector number, as printed on the card.

    Cards from 2020 on (Sword & Shield onwards) print both numbers with three
    digits ("020/189"); older cards don't ("58/102"). This is cosmetic: lookups
    compare the numbers as integers.
    """
    number, total = str(card["number"]), card["set"].get("printedTotal")
    if not number.isdigit() or not total:
        # Subset numbers like "TG05" / "GG01" are printed against the subset,
        # and promo sets often have no printed total. Both are matched by
        # catalog id, so the name just needs to be readable.
        return f"{card['name']} {number}"
    total = str(total)
    if card["set"].get("releaseDate", "")[:4] >= "2020":
        number, total = number.zfill(3), total.zfill(3)
    return f"{card['name']} {number}/{total}"


def _summarize(card: dict, in_binder: dict[str, list[dict]]) -> dict:
    s = card.get("set", {})
    prices = (card.get("tcgplayer") or {}).get("prices") or {}
    name = display_name(card)
    return {
        "id": card["id"],
        "name": card["name"],
        "display_name": name,
        "number": card["number"],
        "rarity": card.get("rarity"),
        "set": {"name": s.get("name"), "code": s.get("ptcgoCode") or s.get("id"),
                "series": s.get("series"), "release_date": s.get("releaseDate")},
        "image_small": (card.get("images") or {}).get("small"),
        "image_large": (card.get("images") or {}).get("large"),
        "finishes": [
            {"key": key, "label": VARIANTS.get(key, key), "market": block.get("market")}
            for key, block in prices.items()
        ],
        "in_binder": in_binder.get(name, []),
    }


def search(text: str, session=None, sleep=None) -> list[dict]:
    """Search the catalog. Raises CatalogError (bad input) or CatalogUnavailable."""
    text = (text or "").strip()
    if len(text) < 2 or len(text) > 60:
        raise CatalogError("Search for 2 to 60 characters.")
    kwargs = {"sleep": sleep} if sleep else {}
    cards = api_search(
        build_query(text), SEARCH_FIELDS, ttl=timedelta(hours=config.get("CACHE_TTL_HOURS")),
        session=session, extra_params={"orderBy": "-set.releaseDate", "pageSize": PAGE_SIZE},
        attempts=3, backoff_s=1.0, **kwargs,
    )
    if cards is None:
        raise CatalogUnavailable("pokemontcg.io didn't respond. It has occasional hiccups; try again in a moment.")
    remember_cards(cards)   # so adding one of these needs no second API call

    # Mark results that are already in the binder (you can still add another copy).
    in_binder: dict[str, list[dict]] = {}
    for c in list_cards():
        in_binder.setdefault(c["name"], []).append({"id": c["id"], "status": c["status"], "grade": c["grade"]})
    return [_summarize(card, in_binder) for card in cards if card.get("set")]


# --- Adding a card from the catalog -------------------------------------------- #

def _lookup(card_id: str, session=None) -> dict:
    if not ID_RE.match(card_id or ""):
        raise CatalogError("Unknown card.")
    cards = api_search(f"id:{card_id}", SEARCH_FIELDS, ttl=None, session=session,
                       attempts=3, backoff_s=1.0)
    if cards is None:
        raise CatalogUnavailable("pokemontcg.io didn't respond. Try again in a moment.")
    if not cards:
        raise CatalogError("That card isn't in the catalog.")
    return cards[0]


def _money(errors: dict, key: str, raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = round(float(raw), 2)
    except (TypeError, ValueError):
        errors[key] = "Enter an amount, like 12.50."
        return None
    if not 0 <= value <= 10_000_000:
        errors[key] = "Enter an amount from 0 to 10,000,000."
    return value


def clean_card_fields(form: dict, finishes: list[str] | None) -> dict:
    """Validate the user-editable card fields (shared by Add card and Edit card).

    `finishes` limits which variants are allowed (None = any known finish).
    Returns clean values ready for the database, or raises CatalogError with
    per-field messages. Buy targets only apply to watched cards and purchase
    info only to owned ones, so the other is cleared.
    """
    errors: dict[str, str] = {}

    status = form.get("status")
    if status not in VALID_STATUSES:
        errors["status"] = "Choose collection or watchlist."
    grade = str(form.get("grade") or "Raw").strip()
    if not GRADE_RE.match(grade):
        errors["grade"] = "Use Raw, or a grade like PSA 10 or BGS 9.5."
    variant = form.get("variant") or None
    if variant is not None and variant not in (finishes if finishes is not None else VARIANTS):
        errors["variant"] = "Choose one of this card's finishes."

    purchase_price = _money(errors, "purchase_price", form.get("purchase_price"))
    target_buy = _money(errors, "target_buy", form.get("target_buy"))
    target_sell = _money(errors, "target_sell", form.get("target_sell"))

    purchase_date = (form.get("purchase_date") or "").strip() or None
    if purchase_date:
        try:
            if date.fromisoformat(purchase_date) > date.today():
                errors["purchase_date"] = "That date is in the future."
        except ValueError:
            errors["purchase_date"] = "Use a date like 2026-10-19."
    notes = (form.get("notes") or "").strip()[:200] or None

    # Raw cards: which condition the copy is in (None = Near Mint). Graded
    # cards' condition is their grade, so it's cleared for them.
    condition = form.get("condition") or None
    if condition is not None and condition not in CONDITIONS:
        errors["condition"] = "Choose a condition from the list."
    if grade != "Raw" or condition == "Near Mint":
        condition = None

    acquired = form.get("acquired") or "bought"
    if acquired not in ("bought", "pulled", "gift"):
        errors["acquired"] = "Choose bought, pulled from a pack, or gift / trade."
    # A pulled card's cost is the pack it came from; blank means you don't count it ($0).
    if acquired in ("pulled", "gift") and purchase_price is None and "purchase_price" not in errors:
        purchase_price = 0.0

    if errors:
        raise CatalogError("Some fields need fixing.", errors)

    owned = status == "owned"
    return {
        "acquired": acquired if owned else None,
        "condition": condition,
        "status": status, "grade": grade, "variant": variant,
        "target_buy": None if owned else target_buy,
        "target_sell": target_sell,
        "purchase_price": purchase_price if owned else None,
        "purchase_date": purchase_date if owned else None,
        "notes": notes,
    }


def card_from_catalog(card_id: str, form: dict, session=None) -> int:
    """Validate the add-card form and insert the card. Returns the new card id."""
    catalog = _lookup(card_id, session)
    finishes = list(((catalog.get("tcgplayer") or {}).get("prices") or {}).keys())
    fields = clean_card_fields(form, finishes)
    if fields["notes"] is None:
        fields["notes"] = f"{catalog['set']['name']} · {catalog.get('rarity') or 'card'}"
    return add_card(
        name=display_name(catalog),
        set_code=catalog["set"].get("ptcgoCode") or catalog["set"]["id"],
        image_url=(catalog.get("images") or {}).get("small"),
        catalog_id=catalog["id"],
        **fields,
    )


def update_from_form(card_id: int, form: dict) -> None:
    """Validate the Edit card form and save it. Any finish is allowed here: the
    catalog's list of priced finishes can lag (JustTCG may price one it lacks)."""
    if not any(c["id"] == card_id for c in list_cards()):
        raise CatalogError(f"no card with id {card_id}")
    update_card(card_id, clean_card_fields(form, None))
