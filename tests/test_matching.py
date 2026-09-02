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


def test_expired_option_treated_as_total_loss_at_zero_sell_price(transactions):
    result = match_transactions(transactions)
    wxyz = next(t for t in result.closed_trades if t.ticker == "WXYZ")
    assert wxyz.sell_price == 0.0
    assert wxyz.quantity == 100  # 1 contract * 100


def test_matches_plain_share_buy_and_sell(transactions):
    result = match_transactions(transactions)
    oldco = next(t for t in result.closed_trades if t.ticker == "OLDCO")
    assert oldco.equity_type == "Shares"
    assert oldco.quantity == 100
    assert oldco.cost_price == pytest.approx(20.00)
    assert oldco.sell_price == pytest.approx(24.99)


def test_no_unmatched_or_open_positions_left_in_clean_fixture(transactions):
    result = match_transactions(transactions)
    assert result.unmatched_closes == []
    assert result.open_positions == []
    assert len(result.closed_trades) == 3


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
        Transaction(datetime(2026, 1, 1), "Buy", "BAZ", "", 50, 10.0, 0.0, -500.0),
        Transaction(datetime(2026, 1, 2), "Buy", "BAZ", "", 50, 12.0, 0.0, -600.0),
        Transaction(datetime(2026, 1, 10), "Sell", "BAZ", "", 80, 15.0, 0.0, 1200.0),
    ]
    result = match_transactions(txns)
    assert len(result.closed_trades) == 2
    first, second = sorted(result.closed_trades, key=lambda t: t.cost_price)
    assert first.quantity == 50
    assert first.cost_price == 10.0
    assert second.quantity == 30
    assert second.cost_price == 12.0
