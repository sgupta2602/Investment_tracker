"""Roll-ups mirroring the sheet's 'Monthly Performance' and
'Gains / Losses / Tax by term' summary blocks (rows 26-32)."""
from __future__ import annotations

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


def performance_by_upload(all_trades: list[dict], uploads: list[dict]) -> list[dict]:
    """One performance snapshot per upload, oldest first -- feeds the
    Overview dashboard's per-period chart."""
    by_upload: dict[int, list[dict]] = {}
    for t in all_trades:
        by_upload.setdefault(t["upload_id"], []).append(t)

    ordered_uploads = sorted(uploads, key=lambda u: u["id"])
    series = []
    for u in ordered_uploads:
        trades = by_upload.get(u["id"], [])
        perf = monthly_performance(trades)
        series.append({"upload_id": u["id"], "label": u["filename"], **perf})
    return series
