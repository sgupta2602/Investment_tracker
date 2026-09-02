"""Bonus 'Income' view: dividends, ADR fees, foreign tax, and transfers.
These never fed into the sheet's gain/loss engine, but they're sitting
right there in the CSV so we surface them for context."""
from __future__ import annotations

from app.parsing import INCOME_ACTIONS, Transaction


def extract_income_events(transactions: list[Transaction]) -> list[dict]:
    events = [t for t in transactions if t.action in INCOME_ACTIONS]
    events.sort(key=lambda t: t.date)
    return [
        {
            "date": t.date,
            "action": t.action,
            "symbol": t.symbol,
            "description": t.description,
            "amount": t.amount,
        }
        for t in events
    ]


def income_totals(events: list[dict]) -> dict:
    totals: dict[str, float] = {}
    for e in events:
        totals[e["action"]] = totals.get(e["action"], 0.0) + e["amount"]
    totals["Total"] = sum(e["amount"] for e in events)
    return totals
