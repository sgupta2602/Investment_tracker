"""Bonus 'Income' view: dividends, ADR fees, foreign tax, and transfers.
These never fed into the sheet's gain/loss engine, but they're sitting
right there in the CSV so we surface them for context."""
from __future__ import annotations

from app.parsing import INCOME_ACTIONS, Transaction


def _display_label(action: str, amount: float) -> str:
    """Translates raw broker action names into labels a non-broker person
    would actually understand. Sign-aware because both of these actions
    can run either direction in principle, even though every example in
    real statements seen so far has been outbound:
      * MoneyLink Transfer -- Schwab's linked-bank-account transfer
        feature. Negative = money leaving to your bank (a Withdrawal);
        positive = money arriving from your bank (a Deposit).
      * Journal -- an internal transfer to/from a DIFFERENT brokerage
        account (e.g. a family member's), not a bank. Negative = you
        sent funds to that other account; positive = they sent funds
        to you.
    Anything else passes through unchanged (dividends, fees, etc. --
    those names are already clear as-is)."""
    if action == "MoneyLink Transfer":
        return "Deposit" if amount >= 0 else "Withdrawal"
    if action == "Journal":
        return "Journal Transfer (from Another Account)" if amount >= 0 else "Journal Transfer (to Another Account)"
    return action


def extract_income_events(transactions: list[Transaction]) -> list[dict]:
    events = [t for t in transactions if t.action in INCOME_ACTIONS]
    events.sort(key=lambda t: t.date)
    return [
        {
            "date": t.date,
            "action": t.action,
            "label": _display_label(t.action, t.amount),
            "symbol": t.symbol,
            "description": t.description,
            "amount": t.amount,
        }
        for t in events
    ]


def income_totals(events: list[dict]) -> dict:
    totals: dict[str, float] = {}
    for e in events:
        totals[e["label"]] = totals.get(e["label"], 0.0) + e["amount"]
    totals["Total"] = sum(e["amount"] for e in events)
    return totals
