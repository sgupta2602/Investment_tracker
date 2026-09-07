"""Tests for the FIFO matching engine -- the trickiest part of this app,
since it's reconstructing trade-pairs from a flat transaction feed."""
from datetime import datetime
from pathlib import Path

import pytest

from app.matching import match_transactions
from app.parsing import Transaction, parse_transactions_csv

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transactions.csv"


@pytest.fixture
def transactions():
    return parse_transactions_csv(FIXTURE, account="TEST123")


def test_matches_option_buy_to_open_with_sell_to_close(transactions):
    result = match_transactions(transactions)
    abcd = next(t for t in result.closed_trades if t.ticker == "ABCD")
    assert abcd.quantity == 200  # 2 contracts * 100
    assert abcd.equity_type == "Options"
    assert abcd.cost_price == pytest.approx(1.0066, abs=1e-4)
    assert abcd.sell_price == pytest.approx(2.9934, abs=1e-4)
    assert abcd.account == "TEST123"


def test_expired_options_close_automatically_at_zero_realized_value(transactions):
    """An expired contract's Price/Fees/Amount are blank in the broker
    export -- the money parser reads blank as 0.0, so 'Expired' closes the
    position at $0 realized value (a total loss of the premium paid),
    matched against its opening lot exactly like a real sale. Before this
    fix, 'Expired' wasn't a recognized closing action at all, so the
    opening leg was stranded looking like a still-open position even
    though the contract had actually lapsed."""
    result = match_transactions(transactions)
    assert not any(p["symbol"] == "WXYZ 01/16/2026 10.00 P" for p in result.open_positions)
    wxyz = next(t for t in result.closed_trades if t.ticker == "WXYZ")
    assert wxyz.quantity == 100  # 1 contract * 100
    assert wxyz.sell_price == 0.0
    assert wxyz.cost_price > 0  # premium paid, nothing recovered -> a loss once enriched


def test_plain_share_trades_are_ignored_entirely():
    """SCOPE (current pass): only options via Buy to Open / Sell to Close
    are processed. OLDCO's plain Buy/Sell (shares) should not appear
    anywhere -- not closed, not open, not unmatched."""
    txns = parse_transactions_csv(FIXTURE, account="TEST123")
    result = match_transactions(txns)
    assert not any(t.ticker == "OLDCO" for t in result.closed_trades)
    assert not any(p["symbol"] == "OLDCO" for p in result.open_positions)
    assert not any(u["symbol"] == "OLDCO" for u in result.unmatched_closes)


def test_only_the_clean_options_round_trip_closes_in_the_fixture(transactions):
    result = match_transactions(transactions)
    assert len(result.closed_trades) == 2  # ABCD's real close + WXYZ's expiration
    assert {t.ticker for t in result.closed_trades} == {"ABCD", "WXYZ"}
    assert result.open_positions == []
    assert result.unmatched_closes == []


def test_partial_close_leaves_remaining_open_lot():
    txns = [
        Transaction(datetime(2026, 1, 1), "Buy to Open", "FOO 01/01 5.00 C", "", 3, 1.0, 0.0, -300.0, "FOO"),
        Transaction(datetime(2026, 1, 5), "Sell to Close", "FOO 01/01 5.00 C", "", 1, 2.0, 0.0, 200.0, "FOO"),
    ]
    result = match_transactions(txns)
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0].quantity == 100  # 1 contract closed
    assert len(result.open_positions) == 1
    assert result.open_positions[0]["remaining_units"] == 200  # 2 contracts left open


def test_unmatched_close_when_no_opening_leg_present():
    txns = [
        Transaction(datetime(2026, 1, 5), "Sell to Close", "BAR 01/01 5.00 C", "", 1, 2.0, 0.0, 200.0, "BAR"),
    ]
    result = match_transactions(txns)
    assert result.closed_trades == []
    assert len(result.unmatched_closes) == 1
    assert result.unmatched_closes[0]["unmatched_units"] == 100


def test_fifo_splits_close_across_multiple_lots():
    """Two separate opening lots at different prices, closed by one order --
    should produce two closed-trade rows, oldest lot consumed first."""
    txns = [
        Transaction(datetime(2026, 1, 1), "Buy to Open", "BAZ 06/19/2026 5.00 C", "", 50, 10.0, 0.0, -50000.0, "BAZ"),
        Transaction(datetime(2026, 1, 2), "Buy to Open", "BAZ 06/19/2026 5.00 C", "", 50, 12.0, 0.0, -60000.0, "BAZ"),
        Transaction(datetime(2026, 1, 10), "Sell to Close", "BAZ 06/19/2026 5.00 C", "", 80, 15.0, 0.0, 120000.0, "BAZ"),
    ]
    result = match_transactions(txns)
    assert len(result.closed_trades) == 2
    first, second = sorted(result.closed_trades, key=lambda t: t.cost_price)
    assert first.quantity == 5000   # 50 contracts * 100
    assert first.cost_price == 10.0
    assert second.quantity == 3000  # 30 contracts * 100
    assert second.cost_price == 12.0


def test_adjusted_contract_closes_with_clean_ticker_and_is_flagged():
    """An OCC-adjusted contract (AZN1) should still match FIFO normally
    against its own opening lot, but the resulting ClosedTrade.ticker
    should be the clean underlying (AZN, not AZN1), with is_adjusted=True
    so the UI can flag it without polluting ticker search/grouping."""
    from app.parsing import parse_option_symbol

    open_fields = parse_option_symbol("AZN1 03/20/2026 90.00 C")
    close_fields = parse_option_symbol("AZN1 03/20/2026 90.00 C")
    txns = [
        Transaction(datetime(2026, 1, 1), "Buy to Open", "AZN1 03/20/2026 90.00 C", "", 3, 5.0, 0.0, -1500.0, **open_fields),
        Transaction(datetime(2026, 3, 20), "Sell to Close", "AZN1 03/20/2026 90.00 C", "", 3, 2.32, 2.00, 694.0, **close_fields),
    ]
    result = match_transactions(txns)
    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade.ticker == "AZN"  # clean, unadjusted ticker -- not "AZN1"
    assert trade.is_adjusted is True
