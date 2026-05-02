"""SQLite schema definitions for trades, signals, positions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

POSITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    position_id   TEXT PRIMARY KEY,
    entry_price   REAL NOT NULL,
    entry_time    INTEGER NOT NULL,
    entry_size    REAL NOT NULL,
    current_price REAL,
    ma_at_entry   REAL,
    ad_at_entry   REAL,
    sl            REAL NOT NULL,
    tp1           REAL NOT NULL,
    tp2           REAL NOT NULL,
    tp3           REAL NOT NULL,
    tp_targets_remaining TEXT NOT NULL,
    status        TEXT NOT NULL,
    notes         TEXT,
    updated_at    INTEGER NOT NULL
);
"""

SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     INTEGER NOT NULL,
    entry_price   REAL NOT NULL,
    sl            REAL NOT NULL,
    tp1           REAL NOT NULL,
    tp2           REAL NOT NULL,
    tp3           REAL NOT NULL,
    position_size REAL NOT NULL,
    confidence    REAL NOT NULL,
    fired         INTEGER NOT NULL,
    reason        TEXT
);
"""

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id   TEXT NOT NULL,
    event         TEXT NOT NULL,
    timestamp     INTEGER NOT NULL,
    price         REAL NOT NULL,
    size          REAL NOT NULL,
    pnl           REAL,
    rule_matched  TEXT,
    notes         TEXT
);
"""

INDEX_STMTS = (
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);",
    "CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id);",
    "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);",
)


def init_database(db_path: str | Path) -> None:
    """Create all tables / indexes if absent. Idempotent."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        cur = conn.cursor()
        cur.execute(POSITIONS_SCHEMA)
        cur.execute(SIGNALS_SCHEMA)
        cur.execute(TRADES_SCHEMA)
        for stmt in INDEX_STMTS:
            cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()
