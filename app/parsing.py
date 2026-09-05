"""Parses a broker (Schwab-style) transaction CSV export into clean
Transaction records. Handles the two messy bits brokers love to throw at you:

1. Option symbols like "AMZN 08/21/2026 245.00 C" that pack underlying,
   expiration, strike and right into one string.
2. Settlement-vs-effective dates like "08/24/2026 as of 08/21/2026" on
   Expired rows -- we want the *effective* (as-of) date, not settlement.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

OPTION_SYMBOL_RE = re.compile(
    r"^(?P<underlying>\S+)\s+(?P<expiration>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<strike>[\d.]+)\s+(?P<right>[CP])$"
)

# SCOPE (user decision, current pass): options contracts only, opened and
# closed cleanly within tracked history. "Buy"/"Sell" (plain share trades)
# and "Expired" (contract lapsed instead of being sold) are deliberately
# excluded for now -- easy to widen back later by adding to these sets.
OPENING_ACTIONS = {"Buy to Open"}
CLOSING_ACTIONS = {"Sell to Close"}
# Actions that are pure cash/income events, not trades.
INCOME_ACTIONS = {
    "Cash Dividend",
    "Qualified Dividend",
    "ADR Mgmt Fee",
    "Foreign Tax Paid",
    "MoneyLink Transfer",
}


@dataclass
class Transaction:
    date: datetime
    action: str
    symbol: str
    description: str
    quantity: float
    price: float
    fees: float
    amount: float

    # Populated for option symbols only.
    underlying: Optional[str] = None
    expiration: Optional[datetime] = None
    strike: Optional[float] = None
    right: Optional[str] = None

    # Populated once persisted / reloaded from the DB.
    upload_id: Optional[int] = None
    account: Optional[str] = None

    @property
    def is_option(self) -> bool:
        return self.underlying is not None

    @property
    def display_ticker(self) -> str:
        return self.underlying if self.is_option else self.symbol


def _parse_money(raw: str) -> float:
    raw = (raw or "").strip()
    if not raw:
        return 0.0
    negative = raw.startswith("-")
    cleaned = raw.replace("$", "").replace(",", "").lstrip("-")
    value = float(cleaned) if cleaned else 0.0
    return -value if negative else value


def _parse_date(raw: str) -> datetime:
    """Handles 'MM/DD/YYYY' and 'MM/DD/YYYY as of MM/DD/YYYY' (uses the
    as-of / effective date, which is what actually matters for holding
    period and tax-year purposes)."""
    raw = raw.strip()
    if " as of " in raw:
        raw = raw.split(" as of ")[1].strip()
    return datetime.strptime(raw, "%m/%d/%Y")


def parse_option_symbol(symbol: str) -> dict:
    match = OPTION_SYMBOL_RE.match(symbol.strip())
    if not match:
        return {}
    return {
        "underlying": match.group("underlying"),
        "expiration": datetime.strptime(match.group("expiration"), "%m/%d/%Y"),
        "strike": float(match.group("strike")),
        "right": match.group("right"),
    }


def extract_account_label(filename: str) -> Optional[str]:
    """Broker filenames look like 'Joint_Tenant_XXX939_Transactions_....csv'.
    Pull the masked account label (XXX###) out if present."""
    match = re.search(r"(X{2,}\d+)", filename)
    return match.group(1) if match else None


def parse_transactions_csv(path: Path, account: Optional[str] = None) -> list[Transaction]:
    transactions: list[Transaction] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            symbol = (row.get("Symbol") or "").strip()
            option_fields = parse_option_symbol(symbol) if symbol else {}
            transactions.append(
                Transaction(
                    date=_parse_date(row["Date"]),
                    action=(row.get("Action") or "").strip(),
                    symbol=symbol,
                    description=(row.get("Description") or "").strip(),
                    quantity=_parse_money(row.get("Quantity", "")) or 0.0,
                    price=_parse_money(row.get("Price", "")),
                    fees=_parse_money(row.get("Fees & Comm", "")),
                    amount=_parse_money(row.get("Amount", "")),
                    account=account,
                    **option_fields,
                )
            )
    return transactions
