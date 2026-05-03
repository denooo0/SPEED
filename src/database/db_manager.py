"""High-level DB access wrapping the SQLite connection."""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db_schema import init_database

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Single-file SQLite store for positions / signals / trades."""

    def __init__(self, db_dir: str | Path = "./database/", filename: str = "atlas.db") -> None:
        self.db_path = Path(db_dir) / filename
        init_database(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    # -- positions --------------------------------------------------------
    def save_position(self, pos: Dict[str, Any]) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO positions
            (position_id, entry_price, entry_time, entry_size,
             current_price, ma_at_entry, ad_at_entry,
             sl, tp1, tp2, tp3, tp_targets_remaining,
             status, notes, updated_at, direction)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pos["position_id"],
                pos["entry_price"],
                pos["entry_time"],
                pos["entry_size"],
                pos.get("current_price", pos["entry_price"]),
                pos.get("ma_at_entry"),
                pos.get("ad_at_entry"),
                pos["sl"],
                pos["tp1"],
                pos["tp2"],
                pos["tp3"],
                json.dumps(pos.get("tp_targets_remaining", ["TP1", "TP2", "TP3"])),
                pos.get("status", "OPEN"),
                pos.get("notes"),
                int(time.time() * 1000),
                pos.get("direction", "long"),
            ),
        )
        self.conn.commit()

    def get_trades_for_position(self, position_id: str) -> List[Dict[str, Any]]:
        """Fetch all trade events for a single position. O(events_for_pos), not O(total)."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM trades WHERE position_id = ? ORDER BY timestamp",
            (position_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def update_position(self, position_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "tp_targets_remaining" in fields:
            fields["tp_targets_remaining"] = json.dumps(fields["tp_targets_remaining"])
        fields["updated_at"] = int(time.time() * 1000)
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.cursor()
        cur.execute(
            f"UPDATE positions SET {cols} WHERE position_id = ?",
            (*fields.values(), position_id),
        )
        self.conn.commit()

    def load_open_positions(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM positions WHERE status IN ('OPEN', 'PARTIALLY_CLOSED') ORDER BY entry_time"
        )
        rows = cur.fetchall()
        return [self._row_to_position(r) for r in rows]

    def get_position(self, position_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM positions WHERE position_id = ?", (position_id,))
        row = cur.fetchone()
        return self._row_to_position(row) if row else None

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["tp_targets_remaining"] = json.loads(d["tp_targets_remaining"])
        except (TypeError, json.JSONDecodeError):
            d["tp_targets_remaining"] = []
        return d

    # -- signals ----------------------------------------------------------
    def log_signal(self, signal: Dict[str, Any], fired: bool) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO signals
            (timestamp, entry_price, sl, tp1, tp2, tp3,
             position_size, confidence, fired, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal["timestamp"],
                signal["entry_price"],
                signal["stop_loss"],
                signal["tp1"],
                signal["tp2"],
                signal["tp3"],
                signal["position_size"],
                signal["confidence"],
                1 if fired else 0,
                signal.get("reason"),
            ),
        )
        self.conn.commit()

    # -- trade events -----------------------------------------------------
    def log_trade_event(
        self,
        position_id: str,
        event: str,
        price: float,
        size: float,
        pnl: Optional[float] = None,
        rule_matched: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO trades
            (position_id, event, timestamp, price, size, pnl, rule_matched, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position_id,
                event,
                int(time.time() * 1000),
                price,
                size,
                pnl,
                rule_matched,
                notes,
            ),
        )
        self.conn.commit()

    def get_trades_since(self, since_ms: int) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM trades WHERE timestamp >= ? ORDER BY timestamp",
            (since_ms,),
        )
        return [dict(r) for r in cur.fetchall()]

    # -- maintenance ------------------------------------------------------
    def backup(self) -> Path:
        backup_path = self.db_path.with_suffix(".db.backup")
        shutil.copy(self.db_path, backup_path)
        logger.info("DB backup written to %s", backup_path)
        return backup_path

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
