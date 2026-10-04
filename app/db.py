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
    right TEXT,
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
    gain_per_day REAL,
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

-- Deliberately NOT a column on closed_trades: that table gets fully
-- DELETE+reinserted on every upload/delete (replace_closed_trades()
-- rebuilds cumulative columns from scratch), which would silently wipe
-- any user-typed notes. This table is keyed by a stable natural key
-- (account/ticker/strike/expiration/buy_date/sell_date/quantity, see
-- repository._trade_key) instead of closed_trades.id, so annotations
-- survive rebuilds as long as the same transactions still produce the
-- same trade.
CREATE TABLE IF NOT EXISTS trade_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_key TEXT NOT NULL UNIQUE,
    notes TEXT,
    recommended_by TEXT,
    reason TEXT,
    updated_at TEXT NOT NULL
);

-- Manual "I know what I paid, the statement with the opening trade just
-- isn't uploaded yet" notes for Needs Review's "missing an opening trade"
-- table. Purely informational -- never read by calc.py/matching.py, never
-- shown in Trade Log, never feeds gain/loss or tax. Keyed by a natural key
-- (account + symbol + close date, see repository.unmatched_close_key())
-- rather than anything from matching.py's output, since unmatched_closes
-- itself is recomputed fresh on every request, never persisted. Pruned
-- automatically in main.py._rebuild_closed_trades() the moment the real
-- opening trade shows up and that row stops appearing in Needs Review at
-- all -- see repository.prune_stale_unmatched_close_overrides().
CREATE TABLE IF NOT EXISTS unmatched_close_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    override_key TEXT NOT NULL UNIQUE,
    buy_date TEXT,
    cost_price REAL,
    updated_at TEXT NOT NULL
);

-- Manual notes for the Open Positions tab: Current Price (there's no live
-- market-quote feed in this app, so an unrealized % Gain needs somewhere
-- to get a comparison price from) and free-form Comments. Keyed the same
-- way as unmatched_close_overrides above -- a natural key (account +
-- symbol + open date, see repository.open_position_key()) rather than
-- anything persisted, since open_positions is recomputed fresh from
-- match_transactions() on every request. Pruned automatically once a
-- position is fully closed and stops appearing in Open Positions at all
-- -- see repository.prune_stale_open_position_overrides().
CREATE TABLE IF NOT EXISTS open_position_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    override_key TEXT NOT NULL UNIQUE,
    current_price REAL,
    comments TEXT,
    updated_at TEXT NOT NULL
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
    try:
        conn.execute("ALTER TABLE closed_trades ADD COLUMN right TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise
    try:
        conn.execute("ALTER TABLE closed_trades ADD COLUMN gain_per_day REAL")
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
