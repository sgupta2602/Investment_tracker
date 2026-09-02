"""Turns a flat transaction feed into closed trade-pairs (the rows that
correspond to the Excel sheet's trade log) using FIFO lot matching.

Why FIFO: it's the standard, defensible default for tax-lot accounting
when the broker export doesn't tag specific lots. If Shivanshu's cousin
ever needs to override a specific match, that's a v2 problem (YAGNI).
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.parsing import CLOSING_ACTIONS, OPENING_ACTIONS, Transaction

OPTION_MULTIPLIER = 100


@dataclass
class _Lot:
    open_date: datetime
    remaining_units: float
    unit_price: float
    fee_per_unit: float


@dataclass
class ClosedTrade:
    account: str
    ticker: str
    equity_type: str  # "Options" | "Shares"
    quantity: float
    buy_date: datetime
    sell_date: datetime
    cost_price: float  # fee-inclusive
    sell_price: float  # fee-net
    strike_price: Optional[float] = None
    expiration: Optional[datetime] = None
    upload_id: Optional[int] = None


@dataclass
class MatchResult:
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    open_positions: list[dict] = field(default_factory=list)
    unmatched_closes: list[dict] = field(default_factory=list)


def _lot_key(txn: Transaction) -> str:
    return txn.symbol.strip()


def _units_and_ticker(txn: Transaction) -> tuple[float, str]:
    """Converts a raw transaction quantity into 'underlying share
    equivalent' units (contracts * 100 for options, shares as-is for
    stock) -- matching the sheet's 'Options / Shares Sold' convention."""
    multiplier = OPTION_MULTIPLIER if txn.is_option else 1
    return abs(txn.quantity) * multiplier, txn.display_ticker


def match_transactions(transactions: list[Transaction]) -> MatchResult:
    lots: dict[str, deque[_Lot]] = defaultdict(deque)
    result = MatchResult()

    for txn in sorted(transactions, key=lambda t: t.date):
        key = _lot_key(txn)

        if txn.action in OPENING_ACTIONS:
            units, _ = _units_and_ticker(txn)
            if units <= 0:
                continue
            fee_per_unit = (txn.fees or 0.0) / units
            lots[key].append(_Lot(txn.date, units, txn.price, fee_per_unit))

        elif txn.action in CLOSING_ACTIONS:
            units, ticker = _units_and_ticker(txn)
            if units <= 0:
                continue
            close_price = 0.0 if txn.action == "Expired" else txn.price
            close_fee_per_unit = (txn.fees or 0.0) / units
            effective_sell_price = close_price - close_fee_per_unit

            remaining_to_close = units
            queue = lots[key]
            while remaining_to_close > 1e-9 and queue:
                lot = queue[0]
                take = min(lot.remaining_units, remaining_to_close)
                cost_price = lot.unit_price + lot.fee_per_unit

                result.closed_trades.append(
                    ClosedTrade(
                        account=txn.account or "UNKNOWN",
                        upload_id=txn.upload_id,
                        ticker=ticker,
                        equity_type="Options" if txn.is_option else "Shares",
                        quantity=take,
                        buy_date=lot.open_date,
                        sell_date=txn.date,
                        cost_price=cost_price,
                        sell_price=effective_sell_price,
                        strike_price=txn.strike,
                        expiration=txn.expiration,
                    )
                )

                lot.remaining_units -= take
                remaining_to_close -= take
                if lot.remaining_units <= 1e-9:
                    queue.popleft()

            if remaining_to_close > 1e-9:
                result.unmatched_closes.append(
                    {
                        "date": txn.date,
                        "symbol": txn.symbol,
                        "ticker": ticker,
                        "unmatched_units": remaining_to_close,
                        "reason": "No opening transaction found in this "
                        "upload (likely opened before the statement window).",
                    }
                )

    for key, queue in lots.items():
        for lot in queue:
            result.open_positions.append(
                {
                    "symbol": key,
                    "open_date": lot.open_date,
                    "remaining_units": lot.remaining_units,
                    "unit_price": lot.unit_price,
                }
            )

    return result
