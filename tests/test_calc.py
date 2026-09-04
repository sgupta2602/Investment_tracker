"""Tests for the per-trade formula engine -- a direct port of the Excel
sheet's column logic. These pin down the exact math so nobody accidentally
breaks the tax numbers later."""
from datetime import datetime

import pytest

from app.calc import LONG_TERM_RATE, SHORT_TERM_RATE, enrich_trades


def _trade(**overrides):
    base = {
        "account": "TEST123",
        "ticker": "XYZ",
        "equity_type": "Shares",
        "quantity": 100,
        "buy_date": datetime(2026, 1, 1),
        "sell_date": datetime(2026, 1, 31),
        "cost_price": 10.0,
        "sell_price": 15.0,
        "strike_price": None,
        "expiration": None,
    }
    base.update(overrides)
    return base


def test_realized_value_and_cost_basis_are_quantity_times_price():
    [row] = enrich_trades([_trade()])
    assert row["realized_value"] == 1500.0
    assert row["cost_basis"] == 1000.0
    assert row["gain_loss"] == 500.0


def test_pct_gain_loss_is_gain_over_cost_basis():
    [row] = enrich_trades([_trade()])
    assert row["pct_gain_loss"] == pytest.approx(0.5)


def test_short_term_gain_taxed_at_short_rate():
    [row] = enrich_trades([_trade(sell_date=datetime(2026, 2, 1))])  # 31 days held
    assert row["gain_type"] == "Short"
    assert row["estimated_tax"] == pytest.approx(500.0 * SHORT_TERM_RATE)


def test_long_term_gain_taxed_at_long_rate_after_365_days():
    [row] = enrich_trades([_trade(buy_date=datetime(2024, 1, 1), sell_date=datetime(2026, 1, 2))])
    assert row["gain_type"] == "Long"
    assert row["estimated_tax"] == pytest.approx(500.0 * LONG_TERM_RATE)


def test_losing_trade_owes_no_tax():
    [row] = enrich_trades([_trade(sell_price=5.0)])  # loss
    assert row["gain_loss"] < 0
    assert row["estimated_tax"] == 0.0


def test_cumulative_columns_run_across_rows_in_order():
    rows = enrich_trades(
        [
            _trade(sell_date=datetime(2026, 1, 10)),
            _trade(sell_date=datetime(2026, 1, 20), cost_price=20.0, sell_price=18.0),  # a loss
        ]
    )
    assert rows[0]["cumulative_investment"] == 1000.0
    assert rows[1]["cumulative_investment"] == 1000.0 + 2000.0
    assert rows[0]["cumulative_gain"] == 500.0
    assert rows[1]["cumulative_gain"] == 500.0 + (-200.0)


def test_hold_period_uses_30_day_months_like_the_sheet():
    [row] = enrich_trades([_trade(buy_date=datetime(2026, 1, 1), sell_date=datetime(2026, 1, 31))])
    assert row["hold_period_months"] == pytest.approx(1.0)


def test_break_even_is_strike_plus_cost_price_for_options():
    [row] = enrich_trades([_trade(strike_price=245.0, cost_price=6.5)])
    assert row["break_even"] == pytest.approx(251.5)


def test_break_even_collapses_to_cost_price_for_shares_with_no_strike():
    [row] = enrich_trades([_trade(strike_price=None, cost_price=10.0)])
    assert row["break_even"] == pytest.approx(10.0)
