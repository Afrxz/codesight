"""T4.3 — P&L for owned cards, and the portfolio summary strip math."""

from src.pnl import compute_pnl, portfolio_summary


def test_owned_card_pnl():
    assert compute_pnl("owned", 180.0, 365.0) == (185.0, 102.8)
    assert compute_pnl("owned", 220.0, 200.0) == (-20.0, -9.1)


def test_watched_or_unvalued_cards_are_null():
    assert compute_pnl("watching", None, 598.0) == (None, None)
    assert compute_pnl("owned", None, 300.0) == (None, None)    # no cost basis
    assert compute_pnl("owned", 100.0, None) == (None, None)    # no market data


def _d(name, status, sold_avg, paid):
    pnl, pct = compute_pnl(status, paid, sold_avg)
    return dict(name=name, status=status, sold_avg=sold_avg, purchase_price=paid,
                unrealized_pnl=pnl, unrealized_pnl_pct=pct)


def test_summary_totals_match_per_card_numbers():
    decisions = [
        _d("A", "owned", 365.0, 180.0),
        _d("B", "owned", 120.0, 95.0),
        _d("C", "owned", 290.0, 220.0),
        _d("W", "watching", 598.0, None),   # must be ignored
    ]
    s = portfolio_summary(decisions)
    owned = [d for d in decisions if d["status"] == "owned"]
    assert s["total_value"] == sum(d["sold_avg"] for d in owned) == 775.0
    assert s["total_pnl"] == sum(d["unrealized_pnl"] for d in owned) == 280.0
    assert s["total_cost"] == 495.0
    assert s["total_pnl_pct"] == round(280 / 495 * 100, 1)
    assert s["best"]["name"] == "A" and s["worst"]["name"] == "B"
