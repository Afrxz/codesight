"""Card management CLI.

A thin command-line front end over the `cards` table: add cards, list them,
flip a card's status, record purchase info, and seed a realistic starter set.

Run examples (from the project root):

    python -m src.manage add --name "Charizard ex 199/165" --set 151 \
        --grade "PSA 10" --status watching
    python -m src.manage list
    python -m src.manage list --status owned
    python -m src.manage update-status --id 3 --status owned
    python -m src.manage set-purchase --id 3 --price 180.00 --date 2025-11-02
    python -m src.manage remove --id 3
    python -m src.manage seed

The CLI stays deliberately simple (argparse, no framework) and routes all writes
through small functions that other modules can reuse.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date

from .db import STATUS_OWNED, STATUS_WATCHING, VALID_STATUSES, get_connection, init_db
from .market.pokemontcg_source import VARIANTS


# --- Core operations (importable, testable without the CLI) ---------------- #

def add_card(
    name: str,
    set_code: str,
    grade: str,
    status: str,
    target_buy: float | None = None,
    target_sell: float | None = None,
    purchase_price: float | None = None,
    purchase_date: str | None = None,
    notes: str | None = None,
    image_url: str | None = None,
    variant: str | None = None,
    catalog_id: str | None = None,
    acquired: str | None = None,
    condition: str | None = None,
) -> int:
    """Insert a card and return its new id. Validates `status` and `variant`.

    `catalog_id` is the exact pokemontcg.io id, set when a card is added from
    the binder's search; with it, prices are looked up by id, not by name.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    _check_variant(variant)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO cards (name, set_code, grade, status, target_buy,
                               target_sell, purchase_price, purchase_date, notes,
                               image_url, variant, catalog_id, acquired, condition)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, set_code, grade, status, target_buy, target_sell,
             purchase_price, purchase_date, notes, image_url, variant, catalog_id, acquired, condition),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_cards(status: str | None = None) -> list[sqlite3.Row]:
    """Return all cards, optionally filtered by status, ordered by id."""
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM cards WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cards ORDER BY id").fetchall()
        return rows


def update_status(card_id: int, status: str) -> None:
    """Flip a card between 'watching' and 'owned'."""
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE cards SET status = ? WHERE id = ?", (status, card_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no card with id {card_id}")


def set_purchase(card_id: int, price: float, date: str | None = None) -> None:
    """Record what we paid (and optionally when) for an owned card.

    Setting purchase info implies ownership, so we also flip status to 'owned'
    as a convenience — you buy a card, it's now in your collection.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE cards SET purchase_price = ?, purchase_date = ?, status = ? "
            "WHERE id = ?",
            (price, date, STATUS_OWNED, card_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no card with id {card_id}")


# Fields Edit card may change. Identity (name, set, catalog id, art) comes from
# the catalog and isn't editable, so a card can't drift away from its prices.
EDITABLE_FIELDS = ("status", "grade", "variant", "target_buy", "target_sell",
                   "purchase_price", "purchase_date", "notes", "acquired", "condition")
# Raw-card conditions, named as TCGplayer / JustTCG name them.
CONDITIONS = ("Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged")
# How an owned card was acquired. For 'pulled' (from a pack) and 'gift' (or
# trade), purchase_price is the pack cost / value given up, and may be 0.
ACQUIRED = ("bought", "pulled", "gift")


def update_card(card_id: int, fields: dict) -> None:
    """Update a card's editable fields (validated by the caller)."""
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    if "status" in fields and fields["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    _check_variant(fields.get("variant"))
    if fields.get("condition") not in (None, *CONDITIONS):
        raise ValueError(f"condition must be one of {CONDITIONS}")
    if fields.get("acquired") not in (None, *ACQUIRED):
        raise ValueError(f"acquired must be one of {ACQUIRED}")
    if not fields:
        return
    # Column names come from the EDITABLE_FIELDS whitelist above, never from
    # user input, so building the SET clause this way is safe.
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with get_connection() as conn:
        cur = conn.execute(f"UPDATE cards SET {assignments} WHERE id = ?", (*fields.values(), card_id))
        if cur.rowcount == 0:
            raise ValueError(f"no card with id {card_id}")


def remove_card(card_id: int) -> tuple[str, int]:
    """Permanently delete a card and its price history.

    Returns (card name, number of history rows deleted). The history goes via
    the foreign key's ON DELETE CASCADE, which works because get_connection()
    turns foreign-key enforcement on.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT name FROM cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            raise ValueError(f"no card with id {card_id}")
        history_rows = conn.execute(
            "SELECT COUNT(*) FROM price_history WHERE card_id = ?", (card_id,)
        ).fetchone()[0]
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    return row["name"], history_rows


# A small, realistic starter set: a mix of watched and owned cards, with cost
# basis on the owned ones. Every card is a real printing (verified against
# pokemontcg.io); the prices are plausible cost-basis figures for owned cards.
_IMG = "https://images.pokemontcg.io"
SEED_CARDS: list[dict] = [
    # --- Watching (no purchase info) ---
    dict(name="Charizard ex 199/165", set_code="151", grade="PSA 10",
         status=STATUS_WATCHING, target_buy=420.0, target_sell=650.0,
         notes="Scarlet & Violet 151 SIR", image_url=f"{_IMG}/sv3pt5/199.png"),
    dict(name="Umbreon VMAX 215/203", set_code="EVS", grade="PSA 10",
         status=STATUS_WATCHING, target_buy=500.0,
         notes="Evolving Skies alt art ('Moonbreon')", image_url=f"{_IMG}/swsh7/215.png"),
    dict(name="Pikachu V 157/172", set_code="BRS", grade="PSA 10",
         status=STATUS_WATCHING, target_buy=130.0,
         notes="Brilliant Stars full art", image_url=f"{_IMG}/swsh9/157.png"),
    dict(name="Mew ex 232/091", set_code="PAF", grade="Raw",
         status=STATUS_WATCHING, target_buy=120.0,
         notes="Paldean Fates SIR, raw/ungraded", image_url=f"{_IMG}/sv4pt5/232.png"),
    # --- Owned (with cost basis) ---
    dict(name="Charizard VMAX 020/189", set_code="DAA", grade="PSA 10",
         status=STATUS_OWNED, purchase_price=180.0, purchase_date="2024-12-10",
         target_sell=350.0, notes="Darkness Ablaze", image_url=f"{_IMG}/swsh3/20.png"),
    dict(name="Lugia V 186/195", set_code="SIT", grade="PSA 9",
         status=STATUS_OWNED, purchase_price=95.0, purchase_date="2025-03-21",
         notes="Silver Tempest alt art", image_url=f"{_IMG}/swsh12/186.png"),
    dict(name="Giratina V 186/196", set_code="LOR", grade="PSA 10",
         status=STATUS_OWNED, purchase_price=220.0, purchase_date="2025-01-15",
         target_sell=400.0, notes="Lost Origin alt art", image_url=f"{_IMG}/swsh11/186.png"),
]


def _check_variant(variant: str | None) -> None:
    if variant is not None and variant not in VARIANTS:
        raise ValueError(f"variant must be one of {', '.join(VARIANTS)} (or omitted), got {variant!r}")


def set_variant(card_id: int, variant: str | None) -> None:
    """Set a card's finish (e.g. 'reverseHolofoil'), or None to pick automatically."""
    _check_variant(variant)
    with get_connection() as conn:
        cur = conn.execute("UPDATE cards SET variant = ? WHERE id = ?", (variant, card_id))
        if cur.rowcount == 0:
            raise ValueError(f"no card with id {card_id}")


def set_manual_price(card_id: int, price: float | None) -> None:
    """Set (or clear, with None) your own price for a card.

    It's only used when the market source has no price for the card, e.g. a
    brand-new set that free sources haven't priced yet.
    """
    if price is not None and not (0 <= price <= 10_000_000):
        raise ValueError("price must be between 0 and 10,000,000")
    stamp = date.today().isoformat() if price is not None else None
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE cards SET manual_price = ?, manual_price_date = ? WHERE id = ?",
            (None if price is None else round(price, 2), stamp, card_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no card with id {card_id}")


def set_image(card_id: int, url: str | None) -> None:
    """Set (or clear, with None) the binder art for a card."""
    with get_connection() as conn:
        cur = conn.execute("UPDATE cards SET image_url = ? WHERE id = ?", (url, card_id))
        if cur.rowcount == 0:
            raise ValueError(f"no card with id {card_id}")


def seed(force: bool = False) -> int:
    """Insert the starter set. Refuses to run if cards already exist, unless
    `force=True` (which wipes the tables first) — avoids silent duplicates."""
    init_db()
    with get_connection() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        if existing and not force:
            raise RuntimeError(
                f"{existing} cards already exist; refusing to seed. "
                f"Use --force to wipe and reseed."
            )
        if force:
            # CASCADE on price_history takes care of dependent rows. Resetting
            # the AUTOINCREMENT counter makes reseeded ids start at 1 again.
            conn.execute("DELETE FROM cards")
            conn.execute("DELETE FROM runs")      # the run ledger described the old cards
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('cards', 'price_history')")
    for card in SEED_CARDS:
        add_card(**card)
    return len(SEED_CARDS)


# --- CLI plumbing ---------------------------------------------------------- #

def _print_cards(rows: list[sqlite3.Row]) -> None:
    """Pretty-print cards as an aligned table."""
    if not rows:
        print("(no cards)")
        return
    header = f"{'id':>3}  {'status':<8}  {'grade':<7}  {'set':<5}  {'name':<24}  {'buy$':>7}  {'sell$':>7}  {'paid$':>7}"
    print(header)
    print("-" * len(header))
    for r in rows:
        def money(v):  # compact nullable money formatting
            return f"{v:7.2f}" if v is not None else f"{'-':>7}"
        print(
            f"{r['id']:>3}  {r['status']:<8}  {r['grade']:<7}  {r['set_code']:<5}  "
            f"{r['name']:<24}  {money(r['target_buy'])}  {money(r['target_sell'])}  "
            f"{money(r['purchase_price'])}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage tracked TCG cards.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add a card")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--set", dest="set_code", required=True)
    p_add.add_argument("--grade", required=True)
    p_add.add_argument("--status", choices=VALID_STATUSES, default=STATUS_WATCHING)
    p_add.add_argument("--target-buy", type=float)
    p_add.add_argument("--target-sell", type=float)
    p_add.add_argument("--purchase-price", type=float)
    p_add.add_argument("--purchase-date")
    p_add.add_argument("--notes")
    p_add.add_argument("--image-url", help="card art URL (else try fetch-images)")
    p_add.add_argument("--variant", choices=list(VARIANTS),
                       help="finish, for raw-card prices (default: pick automatically)")

    p_sv = sub.add_parser("set-variant", help="set a card's finish (holofoil, reverseHolofoil, ...)")
    p_sv.add_argument("--id", type=int, required=True)
    p_sv.add_argument("--variant", choices=[*VARIANTS, "auto"], required=True,
                      help="'auto' picks the regular printing")

    p_list = sub.add_parser("list", help="list cards")
    p_list.add_argument("--status", choices=VALID_STATUSES)

    p_us = sub.add_parser("update-status", help="change a card's status")
    p_us.add_argument("--id", type=int, required=True)
    p_us.add_argument("--status", choices=VALID_STATUSES, required=True)

    p_pr = sub.add_parser("set-price", help="set your own price (used when no source has one)")
    p_pr.add_argument("--id", type=int, required=True)
    group = p_pr.add_mutually_exclusive_group(required=True)
    group.add_argument("--price", type=float)
    group.add_argument("--clear", action="store_true")

    p_rm = sub.add_parser("remove", help="delete a card and its price history")
    p_rm.add_argument("--id", type=int, required=True)

    p_sp = sub.add_parser("set-purchase", help="record purchase info (marks owned)")
    p_sp.add_argument("--id", type=int, required=True)
    p_sp.add_argument("--price", type=float, required=True)
    p_sp.add_argument("--date")

    p_seed = sub.add_parser("seed", help="insert the starter set of cards")
    p_seed.add_argument("--force", action="store_true",
                        help="wipe existing cards and reseed")

    p_si = sub.add_parser("set-image", help="set a card's binder art URL")
    p_si.add_argument("--id", type=int, required=True)
    p_si.add_argument("--url", required=True)

    sub.add_parser("fetch-images",
                   help="look up art on pokemontcg.io for cards that have none")

    args = parser.parse_args(argv)
    init_db()  # always ensure schema exists before any command
    try:
        _dispatch(args)
    except (ValueError, RuntimeError) as exc:
        # Expected user errors (unknown id, refusing to reseed...): one clear
        # line and a non-zero exit code, not a traceback.
        parser.exit(1, f"error: {exc}\n")


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "add":
        new_id = add_card(
            name=args.name, set_code=args.set_code, grade=args.grade,
            status=args.status, target_buy=args.target_buy,
            target_sell=args.target_sell, purchase_price=args.purchase_price,
            purchase_date=args.purchase_date, notes=args.notes,
            image_url=args.image_url, variant=args.variant,
        )
        print(f"Added card id {new_id}: {args.name}")
    elif args.command == "set-variant":
        variant = None if args.variant == "auto" else args.variant
        set_variant(args.id, variant)
        print(f"Card {args.id}: variant = {variant or 'auto'}")
    elif args.command == "list":
        _print_cards(list_cards(status=args.status))
    elif args.command == "update-status":
        update_status(args.id, args.status)
        print(f"Card {args.id} -> {args.status}")
    elif args.command == "set-price":
        set_manual_price(args.id, None if args.clear else args.price)
        print(f"Card {args.id}: your price {'cleared' if args.clear else f'= {args.price:.2f}'}")
    elif args.command == "remove":
        name, history_rows = remove_card(args.id)
        print(f"Removed card {args.id}: {name} (and {history_rows} price-history rows)")
    elif args.command == "set-purchase":
        set_purchase(args.id, args.price, args.date)
        print(f"Card {args.id}: purchase_price={args.price} date={args.date} (now owned)")
    elif args.command == "seed":
        n = seed(force=args.force)
        print(f"Seeded {n} cards.")
    elif args.command == "set-image":
        set_image(args.id, args.url)
        print(f"Card {args.id}: image set")
    elif args.command == "fetch-images":
        from .images import find_image_url  # network lookup; import only when used
        for card in list_cards():
            if card["image_url"]:
                continue
            url = find_image_url(card["name"])
            if url:
                set_image(card["id"], url)
            print(f"Card {card['id']} {card['name']}: {url or 'not found'}")


if __name__ == "__main__":
    main()
