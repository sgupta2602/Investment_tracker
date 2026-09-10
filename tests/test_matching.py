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


def test_same_contract_in_two_different_accounts_does_not_cross_match():
    """Regression: _lot_key() used to be symbol-only, so a Buy to Open in
    one account could get matched against a Sell to Close in a totally
    different account that happened to trade the identical contract
    (same ticker/strike/expiration/right) -- producing a phantom trade
    that mixes two unrelated accounts. Must stay two independent lots."""
    common = dict(
        symbol="SHRD 06/19/2026 50.00 C",
        description="CALL SHRD",
        underlying="SHRD",
        expiration=datetime(2026, 6, 19),
        strike=50.0,
        right="C",
    )
    buy_account_a = Transaction(
        date=datetime(2026, 1, 5), action="Buy to Open", quantity=1, price=1.0, fees=0.0,
        amount=-100.0, account="ACCOUNT_A", **common,
    )
    sell_account_b = Transaction(
        date=datetime(2026, 2, 1), action="Sell to Close", quantity=1, price=3.0, fees=0.0,
        amount=300.0, account="ACCOUNT_B", **common,
    )
    result = match_transactions([buy_account_a, sell_account_b])

    # Must NOT have matched into a closed trade across accounts.
    assert result.closed_trades == []
    # Account A's buy is still open -- nothing in Account A closed it.
    assert len(result.open_positions) == 1
    assert result.open_positions[0]["account"] == "ACCOUNT_A"
    # Account B's sell has no opening trade in ITS account -- unmatched.
    assert len(result.unmatched_closes) == 1
    assert result.unmatched_closes[0]["account"] == "ACCOUNT_B"


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


def test_plain_share_buy_sell_matches_alongside_options():
    """Plain share trades (Buy / Sell) use the same FIFO engine as options
    (Buy to Open / Sell to Close). OLDCO's round trip spans a full year
    (Jan 2025 -> Jan 2026), which also exercises the long-term holding
    period path with a real, non-option trade."""
    txns = parse_transactions_csv(FIXTURE, account="TEST123")
    result = match_transactions(txns)
    oldco = next(t for t in result.closed_trades if t.ticker == "OLDCO")
    assert oldco.equity_type == "Shares"
    assert oldco.quantity == 100  # shares, not multiplied by the 100x options factor
    assert oldco.cost_price == pytest.approx(20.0)  # no opening fee in the fixture
    assert oldco.sell_price == pytest.approx(24.99)  # $25.00 - ($1.00 fee / 100 shares)
    assert oldco.buy_date == datetime(2025, 1, 1)
    assert oldco.sell_date == datetime(2026, 1, 5)
    # Not left dangling anywhere else.
    assert not any(p["symbol"] == "OLDCO" for p in result.open_positions)
    assert not any(u["symbol"] == "OLDCO" for u in result.unmatched_closes)


def test_share_trade_never_cross_matches_an_option_on_the_same_underlying():
    """The exact scenario the user was worried about: a plain share Buy
    on TICK and an option Buy to Open on TICK in the same account must
    stay in two completely separate FIFO queues -- a Sell of the shares
    must never close out the option lot, or vice versa."""
    txns = [
        Transaction(datetime(2026, 1, 1), "Buy", "TICK", "", 100, 10.0, 0.0, -1000.0, account="ACCT"),
        Transaction(
            datetime(2026, 1, 2), "Buy to Open", "TICK 06/19/2026 15.00 C", "", 1, 2.0, 0.0, -200.0,
            underlying="TICK", expiration=datetime(2026, 6, 19), strike=15.0, right="C", account="ACCT",
        ),
        Transaction(datetime(2026, 1, 10), "Sell", "TICK", "", 100, 12.0, 0.0, 1200.0, account="ACCT"),
    ]
    result = match_transactions(txns)

    # The share round trip closes...
    assert len(result.closed_trades) == 1
    closed = result.closed_trades[0]
    assert closed.equity_type == "Shares"
    assert closed.quantity == 100
    # ...and the option's opening lot is completely untouched -- still open.
    assert len(result.open_positions) == 1
    open_pos = result.open_positions[0]
    assert open_pos["equity_type"] == "Options"
    assert open_pos["symbol"] == "TICK 06/19/2026 15.00 C"
    assert open_pos["remaining_units"] == 100  # 1 contract * 100, untouched


def test_only_the_clean_options_round_trip_closes_in_the_fixture(transactions):
    result = match_transactions(transactions)
    assert len(result.closed_trades) == 3  # ABCD's real close + WXYZ's expiration + OLDCO's shares
    assert {t.ticker for t in result.closed_trades} == {"ABCD", "WXYZ", "OLDCO"}
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
    # 200 units left at $1.00 opening price = $200 still at stake.
    assert result.open_positions[0]["cost_value"] == pytest.approx(200.0)


def test_unmatched_close_when_no_opening_leg_present():
    txns = [
        Transaction(datetime(2026, 1, 5), "Sell to Close", "BAR 01/01 5.00 C", "", 1, 2.0, 0.0, 200.0, "BAR"),
    ]
    result = match_transactions(txns)
    assert result.closed_trades == []
    assert len(result.unmatched_closes) == 1
    assert result.unmatched_closes[0]["unmatched_units"] == 100


def test_unmatched_close_and_open_position_carry_split_option_fields():
    """Needs Review used to just dump the raw symbol blob. It should carry
    the same parsed ticker/expiration/strike/right/is_adjusted fields the
    Trade Log gets, so the UI can render real columns instead of one string."""
    txns = [
        Transaction(datetime(2026, 1, 5), "Sell to Close", "BAR 01/01/2026 5.00 P", "", 1, 2.0, 0.0, 200.0, "BAR", expiration=datetime(2026, 1, 1), strike=5.0, right="P"),
        Transaction(datetime(2026, 1, 1), "Buy to Open", "FOO1 06/19/2026 10.00 C", "", 1, 3.0, 0.0, -300.0, "FOO", expiration=datetime(2026, 6, 19), strike=10.0, right="C", is_adjusted=True),
    ]
    result = match_transactions(txns)

    unmatched = result.unmatched_closes[0]
    assert unmatched["ticker"] == "BAR"
    assert unmatched["expiration"] == datetime(2026, 1, 1)
    assert unmatched["strike"] == 5.0
    assert unmatched["right"] == "P"
    assert unmatched["is_adjusted"] is False

    open_pos = result.open_positions[0]
    assert open_pos["ticker"] == "FOO"
    assert open_pos["expiration"] == datetime(2026, 6, 19)
    assert open_pos["strike"] == 10.0
    assert open_pos["right"] == "C"
    assert open_pos["is_adjusted"] is True


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
