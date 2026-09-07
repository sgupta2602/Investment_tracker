"""Data-access layer -- keeps main.py free of raw SQL. Everything here
works in terms of the dataclasses/dicts from parsing.py, matching.py,
and calc.py so the rest of the app never sees SQL.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.db import get_conn
from app.parsing import Transaction, parse_option_symbol

_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime(_DATE_FMT) if dt else None


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    return datetime.strptime(raw, _DATE_FMT) if raw else None


def create_upload(filename: str, account: Optional[str]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO uploads (filename, account, uploaded_at) VALUES (?, ?, ?)",
            (filename, account, datetime.now().strftime(_DATE_FMT)),
        )
        return cur.lastrowid


def list_uploads() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM uploads ORDER BY uploaded_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_upload(upload_id: int) -> None:
    """Removes a statement and everything filed under it (raw transactions,
    income events). Deliberately does NOT touch closed_trades here --
    main.py always calls _rebuild_closed_trades() right after, which
    re-matches from scratch across whatever uploads remain. That's the
    only way to correctly un-wind a cross-upload trade (e.g. a Buy to
    Open from a deleted statement that had matched a Sell to Close in a
    statement still on file) without hand-rolling partial-undo logic."""
    with get_conn() as conn:
        conn.execute("DELETE FROM income_events WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM raw_transactions WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))


def save_raw_transactions(upload_id: int, transactions: list[Transaction]) -> None:
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO raw_transactions
               (upload_id, txn_date, action, symbol, description, quantity, price, fees, amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    upload_id,
                    _fmt_dt(t.date),
                    t.action,
                    t.symbol,
                    t.description,
                    t.quantity,
                    t.price,
                    t.fees,
                    t.amount,
                )
                for t in transactions
            ],
        )


def load_all_transactions() -> list[Transaction]:
    """Reloads every transaction ever uploaded, re-attaching the account
    label + upload_id so matching can be re-run across the full history
    (needed so a buy in one month's CSV can match a sell in the next)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT rt.*, u.account AS account
               FROM raw_transactions rt
               JOIN uploads u ON u.id = rt.upload_id
               ORDER BY rt.txn_date"""
        ).fetchall()

    transactions = []
    for r in rows:
        option_fields = parse_option_symbol(r["symbol"] or "")
        transactions.append(
            Transaction(
                date=_parse_dt(r["txn_date"]),
                action=r["action"],
                symbol=r["symbol"] or "",
                description=r["description"] or "",
                quantity=r["quantity"] or 0.0,
                price=r["price"] or 0.0,
                fees=r["fees"] or 0.0,
                amount=r["amount"] or 0.0,
                account=r["account"],
                upload_id=r["upload_id"],
                **option_fields,
            )
        )
    return transactions


def replace_closed_trades(enriched_trades: list[dict]) -> None:
    """Recomputing cumulative columns means the *whole* closed_trades
    table gets rebuilt on every upload. Data volumes here are personal
    -- tiny -- so a full delete+reinsert is simpler and safer than
    trying to patch individual rows (YAGNI on incremental updates)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM closed_trades")
        conn.executemany(
            """INSERT INTO closed_trades
               (upload_id, account, sell_date, ticker, quantity, equity_type,
                expiration, sell_price, strike_price, cost_price, break_even, buy_date,
                realized_value, cost_basis, cumulative_investment,
                hold_period_months, gain_loss, pct_gain_loss, gain_per_month,
                gain_type, cumulative_gain, cumulative_gain_pct, estimated_tax, is_adjusted)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    t["upload_id"],
                    t["account"],
                    _fmt_dt(t["sell_date"]),
                    t["ticker"],
                    t["quantity"],
                    t["equity_type"],
                    _fmt_dt(t.get("expiration")),
                    t["sell_price"],
                    t.get("strike_price"),
                    t["cost_price"],
                    t["break_even"],
                    _fmt_dt(t["buy_date"]),
                    t["realized_value"],
                    t["cost_basis"],
                    t["cumulative_investment"],
                    t["hold_period_months"],
                    t["gain_loss"],
                    t["pct_gain_loss"],
                    t["gain_per_month"],
                    t["gain_type"],
                    t["cumulative_gain"],
                    t["cumulative_gain_pct"],
                    t["estimated_tax"],
                    int(t.get("is_adjusted", False)),
                )
                for t in enriched_trades
            ],
        )


def _row_to_trade_dict(r) -> dict:
    d = dict(r)
    d["sell_date"] = _parse_dt(d["sell_date"])
    d["buy_date"] = _parse_dt(d["buy_date"])
    d["expiration"] = _parse_dt(d["expiration"])
    return d


def load_all_closed_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM closed_trades ORDER BY sell_date"
        ).fetchall()
        return [_row_to_trade_dict(r) for r in rows]


def load_closed_trades_for_upload(upload_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM closed_trades WHERE upload_id = ? ORDER BY sell_date",
            (upload_id,),
        ).fetchall()
        return [_row_to_trade_dict(r) for r in rows]


def save_income_events(upload_id: int, events: list[dict]) -> None:
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO income_events (upload_id, event_date, action, symbol, description, amount)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    upload_id,
                    _fmt_dt(e["date"]),
                    e["action"],
                    e["symbol"],
                    e["description"],
                    e["amount"],
                )
                for e in events
            ],
        )


def load_income_events_for_upload(upload_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM income_events WHERE upload_id = ? ORDER BY event_date",
            (upload_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["date"] = _parse_dt(d.pop("event_date"))
            out.append(d)
        return out


def load_all_income_events() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM income_events ORDER BY event_date"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["date"] = _parse_dt(d.pop("event_date"))
            out.append(d)
        return out
