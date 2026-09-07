"""Tests for cross-trade roll-ups in summary.py."""
from app.summary import performance_by_recommender


def _trade(**overrides):
    base = {
        "ticker": "XYZ",
        "realized_value": 1500.0,
        "cost_basis": 1000.0,
        "gain_loss": 500.0,
        "recommended_by": "",
    }
    base.update(overrides)
    return base


def test_groups_trades_by_recommended_by():
    trades = [
        _trade(ticker="AAPL", recommended_by="Priya", gain_loss=200.0, cost_basis=1000.0),
        _trade(ticker="MSFT", recommended_by="Priya", gain_loss=300.0, cost_basis=1000.0),
        _trade(ticker="TSLA", recommended_by="Amit", gain_loss=-100.0, cost_basis=500.0),
    ]
    rows = performance_by_recommender(trades)
    by_name = {r["name"]: r for r in rows}

    assert by_name["Priya"]["trade_count"] == 2
    assert by_name["Priya"]["gain"] == 500.0
    assert [t["ticker"] for t in by_name["Priya"]["trades"]] == ["MSFT", "AAPL"]  # biggest gain first

    assert by_name["Amit"]["trade_count"] == 1
    assert by_name["Amit"]["gain"] == -100.0


def test_blank_recommended_by_groups_under_unspecified_not_dropped():
    trades = [
        _trade(ticker="AAPL", recommended_by="", gain_loss=100.0),
        _trade(ticker="MSFT", recommended_by="   ", gain_loss=50.0),  # whitespace-only too
    ]
    rows = performance_by_recommender(trades)
    assert len(rows) == 1
    assert rows[0]["name"] == "Unspecified"
    assert rows[0]["gain"] == 150.0
    assert rows[0]["trade_count"] == 2


def test_sorted_by_gain_descending():
    trades = [
        _trade(ticker="LOSS", recommended_by="Amit", gain_loss=-50.0),
        _trade(ticker="WIN", recommended_by="Priya", gain_loss=500.0),
    ]
    rows = performance_by_recommender(trades)
    assert [r["name"] for r in rows] == ["Priya", "Amit"]


def test_group_totals_reconcile_against_grand_total():
    trades = [
        _trade(ticker="A", recommended_by="Priya", gain_loss=200.0),
        _trade(ticker="B", recommended_by="Amit", gain_loss=-75.0),
        _trade(ticker="C", recommended_by="", gain_loss=30.0),
    ]
    rows = performance_by_recommender(trades)
    assert sum(r["gain"] for r in rows) == sum(t["gain_loss"] for t in trades)


def test_trades_within_a_group_ranked_biggest_gain_first_for_drilldown():
    trades = [
        _trade(ticker="SMALL_WIN", recommended_by="Priya", gain_loss=50.0),
        _trade(ticker="BIG_WIN", recommended_by="Priya", gain_loss=900.0),
        _trade(ticker="A_LOSS", recommended_by="Priya", gain_loss=-40.0),
    ]
    [row] = performance_by_recommender(trades)
    assert [t["ticker"] for t in row["trades"]] == ["BIG_WIN", "SMALL_WIN", "A_LOSS"]
