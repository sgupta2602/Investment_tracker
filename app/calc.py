"""Per-trade financial formulas -- a straight port of the Excel sheet's
column logic (L through V), operating on already-matched closed trades.

Tax rates are fixed per user decision:
  * Long-term (held > 365 days): 20%
  * Short-term: 37%
Only applied to *gains* (a losing trade owes no tax), matching the sheet.
"""
from __future__ import annotations

LONG_TERM_RATE = 0.20
SHORT_TERM_RATE = 0.37
LONG_TERM_THRESHOLD_DAYS = 365


def _gain_type(buy_date, sell_date) -> str:
    return "Long" if (sell_date - buy_date).days > LONG_TERM_THRESHOLD_DAYS else "Short"


def _tax_rate(gain_type: str) -> float:
    return LONG_TERM_RATE if gain_type == "Long" else SHORT_TERM_RATE


def enrich_trades(trades: list[dict]) -> list[dict]:
    """trades: list of dicts with account, ticker, equity_type, quantity,
    buy_date, sell_date, cost_price, sell_price, strike_price, expiration.
    Must already be sorted by sell_date ascending -- cumulative columns
    depend on row order, exactly like the spreadsheet's running sums."""
    enriched = []
    cumulative_investment = 0.0
    cumulative_gain = 0.0

    for t in trades:
        realized_value = t["quantity"] * t["sell_price"]
        cost_basis = t["quantity"] * t["cost_price"]
        gain_loss = realized_value - cost_basis
        pct_gain_loss = (gain_loss / cost_basis) if cost_basis else 0.0
        hold_period_months = (t["sell_date"] - t["buy_date"]).days / 30
        gain_per_month = (pct_gain_loss / hold_period_months) if hold_period_months else 0.0
        gain_type = _gain_type(t["buy_date"], t["sell_date"])
        # Sheet's "Break Even" (J) = Strike Price + Cost Price, both per-unit.
        # For Shares, Strike Price is blank -- Excel treats a blank cell as 0
        # in addition, so Break Even collapses to just Cost Price. Purely
        # informational: no other formula in the sheet consumes this column.
        break_even = (t.get("strike_price") or 0.0) + t["cost_price"]

        cumulative_investment += cost_basis
        cumulative_gain += gain_loss
        cumulative_gain_pct = (
            (cumulative_gain - cumulative_investment) / cumulative_investment
            if cumulative_investment
            else 0.0
        )
        estimated_tax = gain_loss * _tax_rate(gain_type) if gain_loss > 0 else 0.0

        enriched.append(
            {
                **t,
                "realized_value": realized_value,
                "cost_basis": cost_basis,
                "break_even": break_even,
                "cumulative_investment": cumulative_investment,
                "hold_period_months": hold_period_months,
                "gain_loss": gain_loss,
                "pct_gain_loss": pct_gain_loss,
                "gain_per_month": gain_per_month,
                "gain_type": gain_type,
                "cumulative_gain": cumulative_gain,
                "cumulative_gain_pct": cumulative_gain_pct,
                "estimated_tax": estimated_tax,
            }
        )
    return enriched
