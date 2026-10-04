"""Bonus 'Income' view: dividends and fees. These never fed into the
sheet's gain/loss engine, but they're sitting right there in the CSV
so we surface them for context.

Transfers (MoneyLink / Journal) are cash MOVEMENTS, not income -- they
live on their own Transfers tab (see transfers.py). Both share this
module's extraction/persistence step (extract_cash_events, saved into
the same income_events DB table) since they're both non-trade cash
events with an identical shape; they're only split apart into separate
tabs at display time via filter_income_events() / filter_transfer_events().
"""
from __future__ import annotations

from app.calc import DIVIDEND_LONG_TERM_RATE, DIVIDEND_SHORT_TERM_RATE
from app.parsing import CASH_EVENT_ACTIONS, INCOME_ACTIONS, Transaction


def _display_label(action: str, amount: float) -> str:
    """Translates raw broker action names into labels a non-broker person
    would actually understand. Sign-aware because both transfer actions
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


def extract_cash_events(transactions: list[Transaction]) -> list[dict]:
    """Pulls every non-trade cash event (income AND transfers) out of a
    transaction feed for persistence. Kept as one combined extraction so
    there's a single source of truth for 'what counts as a non-trade
    cash row' -- see filter_income_events() / transfers.filter_transfer_events()
    for the display-time split."""
    events = [t for t in transactions if t.action in CASH_EVENT_ACTIONS]
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


def filter_income_events(events: list[dict]) -> list[dict]:
    """Narrows a combined cash-event list (as loaded from the DB) down
    to true income only -- dividends and fees, no transfers."""
    return [e for e in events if e["action"] in INCOME_ACTIONS]


def event_totals(events: list[dict]) -> dict:
    """Sums any list of cash events by friendly label, plus a grand
    Total row. Generic over income OR transfer events -- both share the
    same {label, amount} shape."""
    totals: dict[str, float] = {}
    for e in events:
        totals[e["label"]] = totals.get(e["label"], 0.0) + e["amount"]
    totals["Total"] = sum(e["amount"] for e in events)
    return totals


def dividend_breakdown(income_events: list[dict]) -> dict:
    """Splits dividend income into the same short/long shape as
    summary.gains_losses_by_term(), because that's genuinely how the IRS
    treats it: 'Qualified Dividend' gets a preferential rate just like a
    long-term capital gain, while everything else here -- plain 'Cash
    Dividend' (non-qualified/ordinary), plus the 'ADR Mgmt Fee' and
    'Foreign Tax Paid' deductions against dividend income -- is taxed
    like ordinary income, the same bucket short-term capital gains fall
    into. Uses its own DIVIDEND_LONG_TERM_RATE/DIVIDEND_SHORT_TERM_RATE
    (25% / 42%) rather than the stock LONG_TERM_RATE/SHORT_TERM_RATE --
    per user decision, these are deliberately different numbers, not a
    copy of the capital-gains rates.

    Returns the same {gains, losses, tax, rate} shape per bucket so
    _tax.html can render both tables' rows with identical markup."""

    def bucket(events: list[dict]) -> tuple[float, float]:
        gains = sum(e["amount"] for e in events if e["amount"] > 0)
        losses = sum(e["amount"] for e in events if e["amount"] < 0)
        return gains, losses

    qualified = [e for e in income_events if e["action"] == "Qualified Dividend"]
    ordinary = [e for e in income_events if e["action"] != "Qualified Dividend"]

    q_gains, q_losses = bucket(qualified)
    o_gains, o_losses = bucket(ordinary)

    return {
        "short": {
            "gains": o_gains,
            "losses": o_losses,
            "tax": (o_gains + o_losses) * DIVIDEND_SHORT_TERM_RATE,
            "rate": DIVIDEND_SHORT_TERM_RATE,
        },
        "long": {
            "gains": q_gains,
            "losses": q_losses,
            "tax": (q_gains + q_losses) * DIVIDEND_LONG_TERM_RATE,
            "rate": DIVIDEND_LONG_TERM_RATE,
        },
    }
