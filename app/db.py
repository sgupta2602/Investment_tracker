"""SQLite storage layer. Plain sqlite3 -- no ORM, this schema is simple enough
that an ORM would just be extra ceremony (YAGNI)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    account TEXT,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    txn_date TEXT NOT NULL,
    action TEXT NOT NULL,
    symbol TEXT,
    description TEXT,
    quantity REAL,
    price REAL,
    fees REAL,
    amount REAL
);

CREATE TABLE IF NOT EXISTS closed_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    account TEXT,
    sell_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    quantity REAL NOT NULL,
    equity_type TEXT NOT NULL,
    expiration TEXT,
    sell_price REAL NOT NULL,
    strike_price REAL,
    cost_price REAL NOT NULL,
    break_even REAL,
    buy_date TEXT NOT NULL,
    realized_value REAL NOT NULL,
    cost_basis REAL NOT NULL,
    cumulative_investment REAL,
    hold_period_months REAL,
    gain_loss REAL,
    pct_gain_loss REAL,
    gain_per_month REAL,
    gain_type TEXT,
    cumulative_gain REAL,
    cumulative_gain_pct REAL,
    estimated_tax REAL,
    is_adjusted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS income_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    event_date TEXT NOT NULL,
    action TEXT NOT NULL,
    symbol TEXT,
    description TEXT,
    amount REAL NOT NULL
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for schema changes made after the table
    already existed on someone's machine. SQLite's ALTER TABLE has no
    ADD COLUMN IF NOT EXISTS, so we just swallow the duplicate-column error
    on databases that already have it (either from CREATE TABLE above on a
    fresh install, or from a previous run of this same migration)."""
    try:
        conn.execute("ALTER TABLE closed_trades ADD COLUMN break_even REAL")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise
    try:
        conn.execute("ALTER TABLE closed_trades ADD COLUMN is_adjusted INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
