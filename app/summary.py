"""Roll-ups mirroring the sheet's 'Monthly Performance' and
'Gains / Losses / Tax by term' summary blocks (rows 26-32)."""
from __future__ import annotations

from datetime import datetime

from app.calc import LONG_TERM_RATE, SHORT_TERM_RATE


def monthly_performance(trades: list[dict]) -> dict:
    realized_investment = sum(t["realized_value"] for t in trades)
    cost_basis = sum(t["cost_basis"] for t in trades)
    gain = sum(t["gain_loss"] for t in trades)
    return {
        "realized_investment": realized_investment,
        "cost_basis": cost_basis,
        "gain": gain,
        "yield_pct": (gain / cost_basis) if cost_basis else 0.0,
    }


def gains_losses_by_term(trades: list[dict]) -> dict:
    def bucket(term: str):
        rows = [t for t in trades if t["gain_type"] == term]
        gains = sum(t["gain_loss"] for t in rows if t["gain_loss"] > 0)
        losses = sum(t["gain_loss"] for t in rows if t["gain_loss"] < 0)
        return gains, losses

    short_gains, short_losses = bucket("Short")
    long_gains, long_losses = bucket("Long")

    return {
        "short": {
            "gains": short_gains,
            "losses": short_losses,
            "tax": (short_gains + short_losses) * SHORT_TERM_RATE,
            "rate": SHORT_TERM_RATE,
        },
        "long": {
            "gains": long_gains,
            "losses": long_losses,
            "tax": (long_gains + long_losses) * LONG_TERM_RATE,
            "rate": LONG_TERM_RATE,
        },
    }


def performance_by_month(trades: list[dict]) -> list[dict]:
    """True calendar month-over-month, grouped by each trade's actual
    sell_date -- NOT by which statement it happened to be uploaded in.
    Replaces the old upload-based 'Gain by Statement Period' grouping,
    which was really just an artifact of how/when statements got
    uploaded (one upload can span multiple months, or one month can be
    split across two uploads if you upload mid-month) rather than a
    real trend line. Spans the entire trade history, oldest month first."""
    by_month: dict[str, list[dict]] = {}
    for t in trades:
        key = t["sell_date"].strftime("%Y-%m")
        by_month.setdefault(key, []).append(t)

    series = []
    for key in sorted(by_month.keys()):
        group = by_month[key]
        perf = monthly_performance(group)
        label = datetime.strptime(key, "%Y-%m").strftime("%b %Y")
        series.append({"month": key, "label": label, "trade_count": len(group), **perf})
    return series


def performance_by_recommender(trades: list[dict]) -> list[dict]:
    """Groups closed trades by the Trade Log's 'Recommended By' field so
    Overview can answer 'who told me about this stock, and how much did
    their picks actually contribute to my P&L' -- the whole point of that
    column existing. Trades nobody's tagged yet land under 'Unspecified'
    (not dropped), so the group totals always reconcile against the
    grand total shown elsewhere on the page.

    Each row carries its full contributing trade list (sorted biggest
    gain first), not just a ticker summary -- Overview renders this as a
    click-to-expand drill-down rather than a flat table, so someone with
    a name tagged on 40 trades doesn't get a wall of comma-separated
    tickers shoved in their face by default."""
    by_name: dict[str, list[dict]] = {}
    for t in trades:
        name = (t.get("recommended_by") or "").strip() or "Unspecified"
        by_name.setdefault(name, []).append(t)

    rows = []
    for name, group in by_name.items():
        perf = monthly_performance(group)
        ranked_trades = sorted(group, key=lambda g: g["gain_loss"], reverse=True)
        rows.append(
            {
                "name": name,
                "trade_count": len(group),
                "trades": ranked_trades,
                **perf,
            }
        )
    return sorted(rows, key=lambda r: r["gain"], reverse=True)
