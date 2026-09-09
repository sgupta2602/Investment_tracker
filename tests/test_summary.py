"""Tests for cross-trade roll-ups in summary.py."""
from datetime import datetime

from app.summary import (
    available_year_months,
    cumulative_gain_series,
    filter_by_months,
    performance_by_month,
    performance_by_recommender,
    performance_by_ticker,
    performance_by_year,
    performance_stats,
    top_bottom_tickers,
)


def _trade(**overrides):
    base = {
        "ticker": "XYZ",
        "realized_value": 1500.0,
        "cost_basis": 1000.0,
        "gain_loss": 500.0,
        "recommended_by": "",
        "buy_date": datetime(2026, 1, 1),
        "sell_date": datetime(2026, 1, 15),
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


def test_available_year_months_combines_trades_and_cash_events_deduped():
    trades = [_trade(sell_date=datetime(2026, 1, 15)), _trade(sell_date=datetime(2026, 3, 5))]
    cash_events = [{"date": datetime(2026, 3, 20)}, {"date": datetime(2026, 6, 1)}]

    months = available_year_months(trades, cash_events)

    # Newest first; March appears once even though both a trade and a cash
    # event fall in it.
    assert months == [
        {"key": "2026-06", "label": "Jun 2026"},
        {"key": "2026-03", "label": "Mar 2026"},
        {"key": "2026-01", "label": "Jan 2026"},
    ]


def test_filter_by_months_keeps_only_selected_non_contiguous_months():
    trades = [
        _trade(ticker="JAN", sell_date=datetime(2026, 1, 15)),
        _trade(ticker="MAR", sell_date=datetime(2026, 3, 5)),
        _trade(ticker="JUN", sell_date=datetime(2026, 6, 30)),
        _trade(ticker="NOV", sell_date=datetime(2026, 11, 1)),
    ]

    kept = filter_by_months(trades, "sell_date", {"2026-01", "2026-06", "2026-11"})

    assert {t["ticker"] for t in kept} == {"JAN", "JUN", "NOV"}  # March correctly excluded


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


def test_groups_by_actual_sell_date_month_not_by_upload():
    """The whole point of the replacement: two trades sold in the same
    calendar month must land in the same bucket even if they came from
    two different statement uploads (or, in the old model, would have
    been split across two upload-based bars)."""
    trades = [
        _trade(ticker="A", sell_date=datetime(2026, 1, 5), gain_loss=100.0),
        _trade(ticker="B", sell_date=datetime(2026, 1, 28), gain_loss=50.0),
        _trade(ticker="C", sell_date=datetime(2026, 2, 3), gain_loss=-30.0),
    ]
    rows = performance_by_month(trades)
    by_month = {r["month"]: r for r in rows}

    assert by_month["2026-01"]["trade_count"] == 2
    assert by_month["2026-01"]["gain"] == 150.0
    assert by_month["2026-02"]["trade_count"] == 1
    assert by_month["2026-02"]["gain"] == -30.0


def test_months_sorted_chronologically_with_human_readable_labels():
    trades = [
        _trade(sell_date=datetime(2026, 3, 1)),
        _trade(sell_date=datetime(2026, 1, 1)),
        _trade(sell_date=datetime(2025, 12, 1)),
    ]
    rows = performance_by_month(trades)
    assert [r["month"] for r in rows] == ["2025-12", "2026-01", "2026-03"]
    assert [r["label"] for r in rows] == ["Dec 2025", "Jan 2026", "Mar 2026"]


def test_yearly_groups_by_actual_sell_date_year_not_by_upload():
    """Same idea as the month version: two trades sold in the same
    calendar year land in one bucket regardless of which statement they
    came from, or which month within the year they happened in."""
    trades = [
        _trade(ticker="A", sell_date=datetime(2025, 1, 5), gain_loss=100.0),
        _trade(ticker="B", sell_date=datetime(2025, 11, 28), gain_loss=50.0),
        _trade(ticker="C", sell_date=datetime(2026, 2, 3), gain_loss=-30.0),
    ]
    rows = performance_by_year(trades)
    by_year = {r["year"]: r for r in rows}

    assert by_year["2025"]["trade_count"] == 2
    assert by_year["2025"]["gain"] == 150.0
    assert by_year["2026"]["trade_count"] == 1
    assert by_year["2026"]["gain"] == -30.0


def test_years_sorted_chronologically_with_year_labels():
    trades = [
        _trade(sell_date=datetime(2026, 3, 1)),
        _trade(sell_date=datetime(2024, 1, 1)),
        _trade(sell_date=datetime(2025, 12, 1)),
    ]
    rows = performance_by_year(trades)
    assert [r["year"] for r in rows] == ["2024", "2025", "2026"]
    assert [r["label"] for r in rows] == ["2024", "2025", "2026"]


def test_cumulative_gain_series_is_a_running_total_sorted_by_sell_date():
    trades = [
        _trade(sell_date=datetime(2026, 1, 10), gain_loss=100.0),
        _trade(sell_date=datetime(2026, 1, 5), gain_loss=50.0),  # out of order on purpose
        _trade(sell_date=datetime(2026, 1, 20), gain_loss=-30.0),
    ]
    points = cumulative_gain_series(trades)
    assert [p["value"] for p in points] == [50.0, 150.0, 120.0]


def test_cumulative_gain_series_recomputes_fresh_not_from_a_stored_column():
    """The whole reason this function exists instead of reading a
    trade's own 'cumulative_gain' field: that field (if present) might
    be a running total over a DIFFERENT, larger set of trades (e.g. the
    full multi-account history) than whatever subset got passed in
    here. This must ignore any such field entirely and derive the
    running total purely from gain_loss over the given list."""
    trades = [
        _trade(sell_date=datetime(2026, 1, 5), gain_loss=100.0, cumulative_gain=99999.0),
        _trade(sell_date=datetime(2026, 1, 10), gain_loss=50.0, cumulative_gain=99999.0),
    ]
    points = cumulative_gain_series(trades)
    assert [p["value"] for p in points] == [100.0, 150.0]


def test_performance_stats_win_rate_and_averages():
    trades = [
        _trade(gain_loss=100.0, buy_date=datetime(2026, 1, 1), sell_date=datetime(2026, 1, 11)),  # 10 days
        _trade(gain_loss=-50.0, buy_date=datetime(2026, 1, 1), sell_date=datetime(2026, 1, 31)),  # 30 days
        _trade(gain_loss=200.0, buy_date=datetime(2026, 1, 1), sell_date=datetime(2026, 1, 21)),  # 20 days
    ]
    stats = performance_stats(trades)
    assert stats["trade_count"] == 3
    assert stats["win_count"] == 2
    assert stats["loss_count"] == 1
    assert stats["win_rate_pct"] == 2 / 3
    assert stats["avg_gain_per_trade"] == (100.0 - 50.0 + 200.0) / 3
    assert stats["avg_hold_days"] == (10 + 30 + 20) / 3


def test_performance_stats_empty_trades_no_division_by_zero():
    stats = performance_stats([])
    assert stats["trade_count"] == 0
    assert stats["win_rate_pct"] == 0.0
    assert stats["avg_gain_per_trade"] == 0.0
    assert stats["avg_hold_days"] == 0.0


def test_performance_by_ticker_groups_and_sorts_by_gain():
    trades = [
        _trade(ticker="AAPL", gain_loss=100.0),
        _trade(ticker="AAPL", gain_loss=-20.0),
        _trade(ticker="MSFT", gain_loss=500.0),
    ]
    rows = performance_by_ticker(trades)
    assert [r["ticker"] for r in rows] == ["MSFT", "AAPL"]

    aapl = next(r for r in rows if r["ticker"] == "AAPL")
    assert aapl["trade_count"] == 2
    assert aapl["gain"] == 80.0
    assert aapl["win_rate_pct"] == 0.5


def _ticker_row(ticker: str, gain: float) -> dict:
    return {"ticker": ticker, "trade_count": 1, "gain": gain, "win_rate_pct": 1.0 if gain > 0 else 0.0}


def test_top_bottom_tickers_splits_sorted_list_correctly():
    # Already sorted descending, as performance_by_ticker() produces.
    rows = [_ticker_row(t, g) for t, g in [
        ("A", 1000), ("B", 800), ("C", 600), ("D", 400), ("E", 200),
        ("F", -100), ("G", -300), ("H", -500), ("I", -700), ("J", -900),
    ]]
    top, bottom = top_bottom_tickers(rows, n=5)
    assert [r["ticker"] for r in top] == ["A", "B", "C", "D", "E"]
    # Worst first: J is the single biggest loser.
    assert [r["ticker"] for r in bottom] == ["J", "I", "H", "G", "F"]


def test_top_bottom_tickers_no_overlap_with_fewer_than_2n_tickers():
    """With only 7 distinct tickers and n=5, a naive rows[:5] + rows[-5:]
    would show 3 tickers in both lists -- this must stay disjoint."""
    rows = [_ticker_row(t, g) for t, g in [
        ("A", 500), ("B", 400), ("C", 300), ("D", 200), ("E", 100), ("F", -50), ("G", -200),
    ]]
    top, bottom = top_bottom_tickers(rows, n=5)
    top_symbols = {r["ticker"] for r in top}
    bottom_symbols = {r["ticker"] for r in bottom}
    assert top_symbols.isdisjoint(bottom_symbols)
    assert top_symbols == {"A", "B", "C", "D", "E"}
    assert bottom_symbols == {"F", "G"}


def test_top_bottom_tickers_empty_input():
    top, bottom = top_bottom_tickers([], n=5)
    assert top == []
    assert bottom == []
