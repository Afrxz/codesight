"""Collection-level review: the agent looks at all your cards together.

Per-card decisions can't see things like "most of your value is one card" or
"which watchlist buys fit this month's budget". After each run, `review`:

1. computes the collection facts in code (value, concentration, cards missing
   a purchase price, sells to consider, buy candidates),
2. plans the buying budget (BUY_BUDGET) greedily: strongest buy signals
   first, skipping anything that doesn't fit what's left,
3. asks the LLM for a short briefing over those facts (one JSON call,
   validated), or writes a rule-based briefing when there's no LLM.

As everywhere else, every number comes from code; the LLM only words it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field

from . import config

log = logging.getLogger(__name__)

CONCENTRATION_WARN_PCT = 50.0   # one card worth more than this share of the collection


class Briefing(BaseModel):
    summary: str = Field(min_length=10, max_length=600)
    highlights: list[str] = Field(default_factory=list, max_length=5)


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def collection_facts(decisions: Sequence, cards: Sequence[Mapping]) -> dict:
    by_id = {c["id"]: c for c in cards}
    owned = [d for d in decisions if d.status == "owned"]
    valued = [d for d in owned if d.sold_avg is not None]
    total = round(sum(d.sold_avg for d in valued), 2)

    largest = max(valued, key=lambda d: d.sold_avg, default=None)
    share = round(largest.sold_avg / total * 100, 1) if largest and total else None

    budget = config.get("BUY_BUDGET") or 0.0
    candidates = sorted(
        [d for d in decisions if d.status == "watching" and d.verdict == "buy"
         and (d.best_listing or d.sold_avg) is not None],
        key=lambda d: ({"high": 0, "medium": 1, "low": 2}[d.confidence], d.vs_market_pct or 0))
    plan, left = [], budget
    for d in candidates:
        price = d.best_listing if d.best_listing is not None else d.sold_avg
        if budget and price <= left:
            plan.append({"card_id": d.card_id, "name": d.name, "price": price})
            left = round(left - price, 2)

    return {
        "owned_cards": len(owned),
        "watched_cards": sum(d.status == "watching" for d in decisions),
        "collection_value": total,
        "largest_card": largest.name if largest else None,
        "largest_card_share_pct": share,
        "concentrated": share is not None and share >= CONCENTRATION_WARN_PCT and len(valued) > 1,
        "missing_purchase_price": [d.name for d in owned if by_id.get(d.card_id) and
                                   by_id[d.card_id]["purchase_price"] is None],
        # Pulled from packs / gifts: their cost is known (the pack, often $0), so
        # there's nothing to "add what you paid" for; the whole value is gain.
        "pulled_or_gifted": [d.name for d in owned if by_id.get(d.card_id) and
                             by_id[d.card_id]["acquired"] in ("pulled", "gift")],
        "no_market_price": [d.name for d in decisions if d.sold_avg is None],
        "sells": [{"name": d.name, "reason": d.reason} for d in decisions if d.verdict == "sell"],
        "buys": [{"name": d.name, "confidence": d.confidence} for d in candidates],
        "buy_budget": budget or None,
        "budget_plan": plan,
        "budget_left": left if budget else None,
        "falling_owned": [d.name for d in owned if d.trend_30d == "down"],
    }


def rule_briefing(f: Mapping) -> Briefing:
    """A plain, factual briefing when there's no LLM (or it fails)."""
    if not f["owned_cards"] and not f["watched_cards"]:
        return Briefing(summary="Your binder is empty. Add cards with + Add card to get started.")
    parts = [f"Your {f['owned_cards']} owned card(s) are worth {_usd(f['collection_value'])} at market prices."]
    if f["sells"]:
        parts.append(f"{len(f['sells'])} sell signal(s): {', '.join(s['name'] for s in f['sells'])}.")
    if f["buys"]:
        parts.append(f"{len(f['buys'])} buy signal(s) on your watchlist.")
    if not f["sells"] and not f["buys"]:
        parts.append("No buy or sell signals today.")
    highlights = []
    if f["concentrated"]:
        highlights.append(f"{f['largest_card']} is {f['largest_card_share_pct']:.0f}% of your collection's value.")
    if f["missing_purchase_price"]:
        highlights.append("Add what you paid for " + ", ".join(f["missing_purchase_price"][:3])
                          + " to get profit/loss and sell signals.")
    if f["budget_plan"]:
        spend = sum(p["price"] for p in f["budget_plan"])
        highlights.append(f"Within your {_usd(f['buy_budget'])} budget: "
                          + ", ".join(p["name"] for p in f["budget_plan"]) + f" ({_usd(spend)}).")
    if f["falling_owned"]:
        highlights.append("Falling over 30 days: " + ", ".join(f["falling_owned"][:3]) + ".")
    if f["no_market_price"]:
        highlights.append("No market price yet for " + ", ".join(f["no_market_price"][:3]) + ".")
    return Briefing(summary=" ".join(parts), highlights=highlights[:5])


BRIEFING_PROMPT = """\
You are a disciplined Pokémon card portfolio assistant giving decision SUPPORT
(not financial advice). You get facts about the owner's whole collection as
JSON; every number is already computed — use them as given, never invent any.

Write a short daily briefing: a 2-3 sentence summary of where the collection
stands and what deserves attention, plus up to 4 short highlights (concrete,
actionable: concentration risk, sells to consider, how to use the buying
budget per budget_plan, cards missing a purchase price). Cards in
pulled_or_gifted came from packs or gifts: their value is all gain and they
never need a purchase price, so don't ask for one. No fluff. Write in
plain English for the owner: never mention field names, JSON, or "null"; if
there's no budget, just don't talk about one unless suggesting setting it.

Respond with JSON only: {"summary": "...", "highlights": ["...", "..."]}
"""


def review(decisions: Sequence, cards: Sequence[Mapping], llm=None) -> dict:
    """The collection review stored with each run (decisions.json -> "portfolio")."""
    facts = collection_facts(decisions, cards)
    briefing, by = None, "rules"
    if llm is not None and (facts["owned_cards"] or facts["watched_cards"]):
        try:
            from .decide import _extract_json
            # Leave out empty fields: the model shouldn't see (or echo) "null".
            shown = {k: v for k, v in facts.items() if v not in (None, [], "")}
            briefing = Briefing.model_validate(_extract_json(llm(BRIEFING_PROMPT, json.dumps(shown, default=str))))
            by = "llm"
        except Exception as exc:
            log.warning("Collection briefing via LLM failed (%s); using rules", exc)
    briefing = briefing or rule_briefing(facts)
    return {"facts": facts, "summary": briefing.summary, "highlights": briefing.highlights, "decided_by": by}
