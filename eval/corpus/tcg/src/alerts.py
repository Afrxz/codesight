"""Change detection: the agent tells you when something meaningful happens.

After every run, `detect` compares the new decisions with the previous run
and with recent price history, and creates alerts for:

    price_move      the price moved >= ALERT_MOVE_PCT over the last ~7 days
    verdict_change  a card became a BUY or SELL (or stopped being one)
    target_hit      an owned card reached your sell target / a watched card
                    reached your buy target (only when it newly crosses)
    dip             a watched card is >= ALERT_MOVE_PCT below its 30-day average

A cooldown (one alert per card and kind per COOLDOWN_DAYS) keeps it from
repeating itself every day. Alerts are stored in the `alerts` table and shown
in the binder; if ALERT_WEBHOOK_URL is set they're also posted there.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone

import requests

from . import config
from .db import get_connection
from .memory import get_history

log = logging.getLogger(__name__)

COOLDOWN_DAYS = 7
MOVE_WINDOW_DAYS = 7


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _price_a_week_ago(card_id: int, source: str) -> float | None:
    """The price from the snapshot closest to 7 days ago (between 5 and 10 days old)."""
    now = datetime.now(timezone.utc)
    best, best_gap = None, None
    for h in get_history(card_id, days=10, source=source):
        if h["sold_avg"] is None:
            continue
        age = (now - datetime.fromisoformat(h["run_at"])).total_seconds() / 86400
        if 5 <= age <= 10 and (best_gap is None or abs(age - MOVE_WINDOW_DAYS) < best_gap):
            best, best_gap = h["sold_avg"], abs(age - MOVE_WINDOW_DAYS)
    return best


def detect(cards: Sequence[Mapping], decisions: Sequence, previous: Mapping[int, Mapping],
           source: str) -> list[dict]:
    """New alerts for this run (not yet stored). `previous` maps card_id -> the
    last run's decision dict, so we only alert on things that changed."""
    move_pct = config.get("ALERT_MOVE_PCT")
    by_id = {c["id"]: c for c in cards}
    found = []

    for d in decisions:
        card = by_id.get(d.card_id)
        if card is None:
            continue
        name = d.name
        before = previous.get(d.card_id) or {}
        price = d.sold_avg

        # 1. Big price move over the last week (from our own history).
        if price is not None:
            week_ago = _price_a_week_ago(d.card_id, source)
            if week_ago:
                change = (price - week_ago) / week_ago * 100
                if abs(change) >= move_pct:
                    word = "up" if change > 0 else "down"
                    found.append({"card_id": d.card_id, "kind": "price_move",
                                  "message": f"{name} is {word} {abs(change):.0f}% this week "
                                             f"({_usd(week_ago)} → {_usd(price)})."})

        # 2. The verdict changed to or from an actionable one.
        old = before.get("verdict")
        if old and old != d.verdict:
            found.append({"card_id": d.card_id, "kind": "verdict_change",
                          "message": f"{name} is now a {d.verdict.upper()} (was {old.upper()}): {d.reason}"})

        # 3. Targets newly reached.
        old_price = before.get("sold_avg")
        if price is not None and card["status"] == "owned" and card["target_sell"]:
            target = card["target_sell"]
            if price >= target and (old_price is None or old_price < target):
                found.append({"card_id": d.card_id, "kind": "target_hit",
                              "message": f"{name} reached your {_usd(target)} sell target ({_usd(price)})."})
        if card["status"] == "watching" and card["target_buy"]:
            target = card["target_buy"]
            now_price = d.best_listing if d.best_listing is not None else price
            old_cmp = before.get("best_listing") if before.get("best_listing") is not None else old_price
            if now_price is not None and now_price <= target and (old_cmp is None or old_cmp > target):
                found.append({"card_id": d.card_id, "kind": "target_hit",
                              "message": f"{name} is at {_usd(now_price)}, at or under your "
                                         f"{_usd(target)} buy target."})

    return found


def detect_dips(cards: Sequence[Mapping], markets: Mapping[int, object]) -> list[dict]:
    """Watched cards well below their own 30-day average (JustTCG reports it)."""
    move_pct = config.get("ALERT_MOVE_PCT")
    found = []
    for c in cards:
        m = markets.get(c["id"])
        if c["status"] != "watching" or m is None or not m.avg_30d or m.sold_avg is None or m.manual:
            continue
        dip = (m.sold_avg - m.avg_30d) / m.avg_30d * 100
        if dip <= -move_pct:
            found.append({"card_id": c["id"], "kind": "dip",
                          "message": f"{c['name']} is {abs(dip):.0f}% below its 30-day average "
                                     f"({_usd(m.sold_avg)} vs {_usd(m.avg_30d)})."})
    return found


def store(new_alerts: Sequence[dict]) -> list[dict]:
    """Save alerts that aren't in their cooldown window. Returns the ones saved."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=COOLDOWN_DAYS)).isoformat(timespec="seconds")
    saved = []
    with get_connection() as conn:
        for a in new_alerts:
            recent = conn.execute(
                "SELECT 1 FROM alerts WHERE card_id IS ? AND kind = ? AND created_at >= ?",
                (a["card_id"], a["kind"], since)).fetchone()
            if recent:
                continue
            cur = conn.execute(
                "INSERT INTO alerts (card_id, kind, message, created_at) VALUES (?, ?, ?, ?)",
                (a["card_id"], a["kind"], a["message"], now.isoformat(timespec="seconds")))
            saved.append({**a, "id": cur.lastrowid})
    return saved


def send(alerts: Sequence[dict], session=None) -> int:
    """Post alerts to ALERT_WEBHOOK_URL, if set. Returns how many were sent.

    The body carries the text as both "content" (Discord) and "text" (Slack),
    so the same payload works with either kind of incoming webhook.
    """
    url = config.get("ALERT_WEBHOOK_URL")
    if not url or not alerts:
        return 0
    session = session or requests.Session()
    text = "Card Binder agent:\n" + "\n".join(f"• {a['message']}" for a in alerts)
    try:
        resp = session.post(url, json={"content": text[:1900], "text": text}, timeout=15)
    except requests.RequestException as exc:
        log.warning("Alert webhook failed: %s", exc)
        return 0
    if not resp.ok:
        log.warning("Alert webhook answered HTTP %d", resp.status_code)
        return 0
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.executemany("UPDATE alerts SET sent_at = ? WHERE id = ?", [(stamp, a["id"]) for a in alerts])
    return len(alerts)


def recent(limit: int = 30) -> list[dict]:
    """Newest alerts first, with the card's name, for the binder."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT a.*, c.name AS card_name FROM alerts a LEFT JOIN cards c ON c.id = a.card_id
            ORDER BY a.created_at DESC, a.id DESC LIMIT ?
            """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def unread_count() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM alerts WHERE read_at IS NULL").fetchone()[0]


def mark_all_read() -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute("UPDATE alerts SET read_at = ? WHERE read_at IS NULL", (stamp,))
