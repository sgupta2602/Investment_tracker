"""Transfers tab: cash MOVEMENTS in/out of this account -- MoneyLink
(linked bank account) transfers and Journal (a different brokerage
account, e.g. a family member's) transfers. Not income (nothing was
earned), and not a trade, so they get their own tab rather than
cluttering either the Income tab or the Trade Log.

Shares extraction/persistence with income.py's extract_cash_events()
since both are non-trade cash events pulled from the same broker
action rows -- only the display-time split happens here.
"""
from __future__ import annotations

from app.parsing import TRANSFER_ACTIONS


def filter_transfer_events(events: list[dict]) -> list[dict]:
    """Narrows a combined cash-event list (as loaded from the DB) down
    to transfers only -- MoneyLink and Journal, no dividends/fees."""
    return [e for e in events if e["action"] in TRANSFER_ACTIONS]
