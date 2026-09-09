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
    ticker: str
    account: str = "UNKNOWN"
    symbol: str = ""
    expiration: Optional[datetime] = None
    strike: Optional[float] = None
    right: Optional[str] = None
    is_adjusted: bool = False


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
    is_adjusted: bool = False


@dataclass
class MatchResult:
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    open_positions: list[dict] = field(default_factory=list)
    unmatched_closes: list[dict] = field(default_factory=list)


def _lot_key(txn: Transaction) -> str:
    """Groups opening/closing legs into the same FIFO queue -- keyed by
    BOTH account and symbol, not symbol alone. Without the account
    component, a Buy to Open in one account could incorrectly get
    matched against a Sell to Close in a completely different account
    that happens to trade the identical contract (same ticker/strike/
    expiration/right), producing a phantom trade that mixes two
    unrelated accounts' positions."""
    return f"{txn.account or 'UNKNOWN'}|{txn.symbol.strip()}"


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
        if not txn.is_option:
            continue  # SCOPE (user decision, current pass): options only
        key = _lot_key(txn)

        if txn.action in OPENING_ACTIONS:
            units, ticker = _units_and_ticker(txn)
            if units <= 0:
                continue
            fee_per_unit = (txn.fees or 0.0) / units
            lots[key].append(
                _Lot(
                    txn.date,
                    units,
                    txn.price,
                    fee_per_unit,
                    ticker=ticker,
                    account=txn.account or "UNKNOWN",
                    symbol=txn.symbol.strip(),
                    expiration=txn.expiration,
                    strike=txn.strike,
                    right=txn.right,
                    is_adjusted=txn.is_adjusted,
                )
            )

        elif txn.action in CLOSING_ACTIONS:
            units, ticker = _units_and_ticker(txn)
            if units <= 0:
                continue
            close_fee_per_unit = (txn.fees or 0.0) / units
            effective_sell_price = txn.price - close_fee_per_unit

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
                        is_adjusted=txn.is_adjusted,
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
                        "account": txn.account or "UNKNOWN",
                        "expiration": txn.expiration,
                        "strike": txn.strike,
                        "right": txn.right,
                        "is_adjusted": txn.is_adjusted,
                        "unmatched_units": remaining_to_close,
                        "reason": "No opening transaction found in this "
                        "upload (likely opened before the statement window).",
                    }
                )

    for key, queue in lots.items():
        for lot in queue:
            result.open_positions.append(
                {
                    "symbol": lot.symbol,
                    "ticker": lot.ticker,
                    "account": lot.account,
                    "expiration": lot.expiration,
                    "strike": lot.strike,
                    "right": lot.right,
                    "is_adjusted": lot.is_adjusted,
                    "open_date": lot.open_date,
                    "remaining_units": lot.remaining_units,
                    "unit_price": lot.unit_price,
                    # How much money is actually at stake in this still-open
                    # position -- units * price paid, same convention as
                    # cost_basis on a closed trade (fee-exclusive here, since
                    # there's no closing fee yet to net against).
                    "cost_value": lot.remaining_units * lot.unit_price,
                }
            )

    return result
