"""Data-access layer -- keeps main.py free of raw SQL. Everything here
works in terms of the dataclasses/dicts from parsing.py, matching.py,
and calc.py so the rest of the app never sees SQL.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.db import get_conn
from app.income import _display_label
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


def transaction_key(t: Transaction) -> str:
    """Deterministic identity for a raw broker transaction row, used to
    skip re-inserting duplicates when a new upload's date range overlaps
    an existing one -- e.g. a broker export style that always starts
    from Jan 1 (so a 'Jan-Nov' download re-includes everything from an
    earlier 'Jan-Sep' upload verbatim). Built purely from the row's own
    content plus account (so the same-looking row in two DIFFERENT
    accounts is correctly treated as two distinct transactions, not a
    duplicate) -- deliberately excludes upload_id, which is exactly the
    thing that differs between the 'same' transaction seen twice.
    Collision risk (two genuinely different real transactions sharing
    every one of these fields) is accepted as a YAGNI trade-off, same
    reasoning as trade_key() above. Numeric fields are cast to float so
    a freshly-parsed row (always float) and one round-tripped through
    SQLite (also always float) can never mismatch on int-vs-float
    string formatting ("1" vs "1.0")."""
    return "|".join(
        str(x)
        for x in [
            t.account,
            _fmt_dt(t.date),
            t.action,
            t.symbol,
            t.description,
            float(t.quantity),
            float(t.price),
            float(t.fees),
            float(t.amount),
        ]
    )


def load_all_transaction_keys() -> set[str]:
    """Every transaction_key() currently in the system, across all
    uploads -- used by the upload route to filter out duplicates from a
    newly-uploaded file before saving it."""
    return {transaction_key(t) for t in load_all_transactions()}


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


# --- User-editable annotations (Notes/Comments, Recommended By, Reason) ---
# Kept in a separate table, keyed by a stable natural key rather than
# closed_trades.id, precisely because closed_trades gets fully rebuilt on
# every upload/delete -- see the trade_annotations table comment in db.py.
ANNOTATION_FIELDS = {"notes", "recommended_by", "reason"}


def trade_key(t: dict) -> str:
    """Deterministic identity for a closed trade that survives a full
    closed_trades rebuild, as long as the same transactions still produce
    the same trade. Collision risk (two genuinely distinct trades sharing
    every one of these fields) is negligible for personal trading data --
    accepted as a YAGNI trade-off rather than inventing a heavier ID
    scheme nothing here actually needs."""
    return "|".join(
        str(x)
        for x in [
            t.get("account"),
            t.get("ticker"),
            t.get("strike_price"),
            _fmt_dt(t.get("expiration")),
            _fmt_dt(t["buy_date"]),
            _fmt_dt(t["sell_date"]),
            t["quantity"],
        ]
    )


def save_trade_annotation_field(key: str, field: str, value: str) -> None:
    if field not in ANNOTATION_FIELDS:
        raise ValueError(f"Unknown annotation field: {field}")
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO trade_annotations (trade_key, {field}, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(trade_key) DO UPDATE SET {field} = excluded.{field}, updated_at = excluded.updated_at""",
            (key, value, datetime.now().strftime(_DATE_FMT)),
        )


def load_all_annotations() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM trade_annotations").fetchall()
        return {r["trade_key"]: dict(r) for r in rows}


def _attach_annotations(trades: list[dict]) -> list[dict]:
    annotations = load_all_annotations()
    for t in trades:
        key = trade_key(t)
        t["trade_key"] = key
        ann = annotations.get(key, {})
        for field in ANNOTATION_FIELDS:
            t[field] = ann.get(field) or ""
    return trades


def load_all_closed_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM closed_trades ORDER BY sell_date"
        ).fetchall()
        return _attach_annotations([_row_to_trade_dict(r) for r in rows])


def load_closed_trades_for_upload(upload_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM closed_trades WHERE upload_id = ? ORDER BY sell_date",
            (upload_id,),
        ).fetchall()
        return _attach_annotations([_row_to_trade_dict(r) for r in rows])


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
    """Joins through uploads for account -- income_events itself has no
    account column (see db.py), since account lives at the statement
    level, not per-row."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ie.*, u.account AS account
               FROM income_events ie
               JOIN uploads u ON u.id = ie.upload_id
               WHERE ie.upload_id = ? ORDER BY ie.event_date""",
            (upload_id,),
        ).fetchall()
        return [_row_to_income_dict(r) for r in rows]


def load_all_income_events() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ie.*, u.account AS account
               FROM income_events ie
               JOIN uploads u ON u.id = ie.upload_id
               ORDER BY ie.event_date"""
        ).fetchall()
        return [_row_to_income_dict(r) for r in rows]


def _row_to_income_dict(r) -> dict:
    """Reapplies income.py's friendly relabeling on every load -- label
    isn't persisted (it's a pure function of action + amount, computed
    fresh both at upload time via extract_income_events() and here on
    reload, rather than duplicating the relabeling rules in two places)."""
    d = dict(r)
    d["date"] = _parse_dt(d.pop("event_date"))
    d["label"] = _display_label(d["action"], d["amount"])
    return d
