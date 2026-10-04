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
    """Each upload also gets a human-friendly period_label -- e.g.
    'Jan 01\u201331, 2026' -- computed from the MIN/MAX transaction date
    actually contained in that upload, not the raw filename or the
    timestamp it happened to be uploaded at. Sorted by that same period
    (newest covered period first), since 'Viewing period' is about what
    date range a statement covers, not the order you got around to
    uploading them in -- so uploading January's statement after
    February's still puts January in the right spot.

    Falls back to the filename for the rare upload with zero
    transactions (fully empty CSV) where there's no date range to show.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT u.*, MIN(rt.txn_date) AS date_from, MAX(rt.txn_date) AS date_to
               FROM uploads u
               LEFT JOIN raw_transactions rt ON rt.upload_id = u.id
               GROUP BY u.id
               ORDER BY date_from DESC, u.uploaded_at DESC"""
        ).fetchall()
        uploads = [dict(r) for r in rows]
    for u in uploads:
        date_from = _parse_dt(u["date_from"])
        date_to = _parse_dt(u["date_to"])
        u["period_label"] = _format_period_label(date_from, date_to, u["filename"])
    return uploads


def _format_period_label(date_from: Optional[datetime], date_to: Optional[datetime], filename: str) -> str:
    if not date_from or not date_to:
        return filename
    if date_from.date() == date_to.date():
        return date_from.strftime("%b %d, %Y")
    if (date_from.year, date_from.month) == (date_to.year, date_to.month):
        return f"{date_from.strftime('%b %d')}\u2013{date_to.strftime('%d, %Y')}"
    if date_from.year == date_to.year:
        return f"{date_from.strftime('%b %d')} \u2013 {date_to.strftime('%b %d, %Y')}"
    return f"{date_from.strftime('%b %d, %Y')} \u2013 {date_to.strftime('%b %d, %Y')}"


def group_uploads_by_account(uploads: list[dict]) -> list[dict]:
    """Splits an already-sorted uploads list into per-account groups for
    an <optgroup>-style dropdown, without re-sorting -- each group keeps
    the same relative (period-descending) order it arrived in."""
    groups: dict[str, list[dict]] = {}
    for u in uploads:
        groups.setdefault(u.get("account") or "Unknown", []).append(u)
    return [{"account": acct, "uploads": items} for acct, items in sorted(groups.items())]


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
                expiration, right, sell_price, strike_price, cost_price, break_even, buy_date,
                realized_value, cost_basis, cumulative_investment,
                hold_period_months, gain_loss, pct_gain_loss, gain_per_day,
                gain_type, cumulative_gain, cumulative_gain_pct, estimated_tax, is_adjusted)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    t["upload_id"],
                    t["account"],
                    _fmt_dt(t["sell_date"]),
                    t["ticker"],
                    t["quantity"],
                    t["equity_type"],
                    _fmt_dt(t.get("expiration")),
                    t.get("right"),
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
                    t["gain_per_day"],
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


# --- Manual notes for Needs Review's "missing an opening trade" table -----
# Purely informational (see the unmatched_close_overrides table comment in
# db.py) -- never read by calc.py or matching.py, never shown in Trade Log.
UNMATCHED_OVERRIDE_FIELDS = {"buy_date", "cost_price"}


def unmatched_close_key(u: dict) -> str:
    """Deterministic identity for one row of match_transactions()'s
    unmatched_closes -- recomputed fresh on every dashboard request (never
    persisted itself), so a manually-typed Buy Date / Cost Price note needs
    its own stable key to survive a page reload. Built from account +
    symbol + close date only -- deliberately excludes unmatched_units, so
    a note stays attached to 'this real-world closing transaction' even if
    a partial re-upload changes how many units are still unmatched for it.
    Same YAGNI collision trade-off as trade_key()/transaction_key() above:
    two genuinely different closes sharing all three fields is accepted as
    negligible risk for personal trading data."""
    return "|".join(str(x) for x in [u.get("account"), u.get("symbol"), _fmt_dt(u.get("date"))])


def save_unmatched_close_override_field(key: str, field: str, value: str) -> None:
    if field not in UNMATCHED_OVERRIDE_FIELDS:
        raise ValueError(f"Unknown override field: {field}")
    # cost_price is stored numerically (REAL column) so it round-trips
    # cleanly if this ever needs formatting -- an empty input clears the
    # field back to NULL rather than storing an invalid empty string.
    db_value: Optional[float | str] = value or None
    if field == "cost_price" and db_value is not None:
        db_value = float(db_value)
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO unmatched_close_overrides (override_key, {field}, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(override_key) DO UPDATE SET {field} = excluded.{field}, updated_at = excluded.updated_at""",
            (key, db_value, datetime.now().strftime(_DATE_FMT)),
        )


def load_all_unmatched_close_overrides() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM unmatched_close_overrides").fetchall()
        return {r["override_key"]: dict(r) for r in rows}


def attach_unmatched_close_overrides(unmatched: list[dict]) -> list[dict]:
    """Adds override_key plus the manually-typed buy_date/cost_price (blank
    string if never set) onto each unmatched-close dict, mirroring how
    _attach_annotations() decorates closed trades for the Trade Log."""
    overrides = load_all_unmatched_close_overrides()
    for u in unmatched:
        key = unmatched_close_key(u)
        u["override_key"] = key
        ov = overrides.get(key, {})
        u["buy_date"] = ov.get("buy_date") or ""
        cost_price = ov.get("cost_price")
        u["cost_price"] = "" if cost_price is None else cost_price
    return unmatched


def prune_stale_unmatched_close_overrides(current_keys: set[str]) -> None:
    """Deletes any manual override whose row no longer shows up in Needs
    Review at all -- i.e. the real opening trade arrived (via a new
    upload) or the close itself went away (via a delete), and matching.py
    resolved it for real. Called from main.py._rebuild_closed_trades()
    right after every upload/delete, using the unmatched_closes list that
    rebuild already computed -- no extra matching pass needed."""
    with get_conn() as conn:
        if not current_keys:
            conn.execute("DELETE FROM unmatched_close_overrides")
            return
        placeholders = ",".join("?" * len(current_keys))
        conn.execute(
            f"DELETE FROM unmatched_close_overrides WHERE override_key NOT IN ({placeholders})",
            tuple(current_keys),
        )


# --- Manual notes for the Open Positions tab (Current Price, Comments) ----
# Purely informational, same spirit as the unmatched-close overrides above
# -- see the open_position_overrides table comment in db.py.
OPEN_POSITION_OVERRIDE_FIELDS = {"current_price", "comments"}


def open_position_key(p: dict) -> str:
    """Deterministic identity for one row of match_transactions()'s
    open_positions -- recomputed fresh on every dashboard request, so a
    manually-typed Current Price / Comments note needs its own stable key
    to survive a page reload. Built from account + symbol + open date,
    same YAGNI collision trade-off as unmatched_close_key() above."""
    return "|".join(str(x) for x in [p.get("account"), p.get("symbol"), _fmt_dt(p.get("open_date"))])


def save_open_position_override_field(key: str, field: str, value: str) -> None:
    if field not in OPEN_POSITION_OVERRIDE_FIELDS:
        raise ValueError(f"Unknown override field: {field}")
    db_value: Optional[float | str] = value or None
    if field == "current_price" and db_value is not None:
        db_value = float(db_value)
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO open_position_overrides (override_key, {field}, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(override_key) DO UPDATE SET {field} = excluded.{field}, updated_at = excluded.updated_at""",
            (key, db_value, datetime.now().strftime(_DATE_FMT)),
        )


def load_all_open_position_overrides() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM open_position_overrides").fetchall()
        return {r["override_key"]: dict(r) for r in rows}


def attach_open_position_overrides(positions: list[dict]) -> list[dict]:
    """Decorates each open position with: override_key + the manually-typed
    current_price/comments (blank if never set), Break Even (same formula
    as calc.py's per-trade column -- strike + unit price for options, just
    unit price for shares), and pct_gain (None until a Current Price has
    been entered, since there's no live quote to compare against)."""
    overrides = load_all_open_position_overrides()
    for p in positions:
        key = open_position_key(p)
        p["override_key"] = key
        ov = overrides.get(key, {})
        current_price = ov.get("current_price")
        p["current_price"] = "" if current_price is None else current_price
        p["comments"] = ov.get("comments") or ""
        p["break_even"] = (p.get("strike") or 0.0) + p["unit_price"]
        if current_price is not None and p["unit_price"]:
            p["pct_gain"] = (current_price - p["unit_price"]) / p["unit_price"]
        else:
            p["pct_gain"] = None
    return positions


def prune_stale_open_position_overrides(current_keys: set[str]) -> None:
    """Deletes any manual Current Price/Comments note whose position no
    longer shows up in Open Positions at all -- i.e. it got fully closed
    out (via a new upload's Sell/Sell to Close/Expired, or the opening
    trade itself was removed via a delete). Called from
    main.py._rebuild_closed_trades() right after every upload/delete,
    using the open_positions list that rebuild already computed."""
    with get_conn() as conn:
        if not current_keys:
            conn.execute("DELETE FROM open_position_overrides")
            return
        placeholders = ",".join("?" * len(current_keys))
        conn.execute(
            f"DELETE FROM open_position_overrides WHERE override_key NOT IN ({placeholders})",
            tuple(current_keys),
        )
