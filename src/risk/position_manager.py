"""Track open positions across restarts; manage scale-out and SL trailing."""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)


@dataclass
class ExitAction:
    position_id: str
    kind: str            # "TP1" | "TP2" | "TP3" | "SL" | "TRAIL"
    price: float
    size: float
    pnl: float


class PositionManager:
    """Persists and updates positions in the SQLite store."""

    def __init__(
        self,
        db: DatabaseManager,
        scale_out_pct: List[float] | None = None,
        on_close: Optional[Any] = None,
    ) -> None:
        self.db = db
        self.scale_out_pct = list(scale_out_pct) if scale_out_pct else [25.0, 25.0, 25.0, 25.0]
        # Optional close hook: called as on_close(position_dict, trade_events)
        # Used to write autopsies on full closure.
        self.on_close = on_close

    # -- lifecycle --------------------------------------------------------
    def open_from_signal(
        self,
        signal: Dict[str, Any],
        ma_at_entry: Optional[float] = None,
        ad_at_entry: Optional[float] = None,
    ) -> Dict[str, Any]:
        position_id = uuid.uuid4().hex
        record = {
            "position_id": position_id,
            "entry_price": signal["entry_price"],
            "entry_time": int(time.time() * 1000),
            "entry_size": signal["position_size"],
            "current_price": signal["entry_price"],
            "ma_at_entry": ma_at_entry,
            "ad_at_entry": ad_at_entry,
            "sl": signal["stop_loss"],
            "tp1": signal["tp1"],
            "tp2": signal["tp2"],
            "tp3": signal["tp3"],
            "tp_targets_remaining": ["TP1", "TP2", "TP3"],
            "status": "OPEN",
            "notes": signal.get("reason"),
        }
        self.db.save_position(record)
        self.db.log_trade_event(
            position_id=position_id,
            event="ENTRY",
            price=signal["entry_price"],
            size=signal["position_size"],
            rule_matched=signal.get("reason"),
        )
        logger.info("Opened position %s @ %.2f size=%.4f", position_id, signal["entry_price"], signal["position_size"])
        return record

    def has_open_position(self) -> bool:
        return bool(self.db.load_open_positions())

    def get_open_positions(self) -> List[Dict[str, Any]]:
        return self.db.load_open_positions()

    # -- monitoring -------------------------------------------------------
    def evaluate(self, position: Dict[str, Any], current_price: float) -> List[ExitAction]:
        actions: List[ExitAction] = []
        remaining = list(position.get("tp_targets_remaining") or [])
        size_remaining = float(position.get("entry_size", 0))
        entry = float(position["entry_price"])

        # Stop loss first
        if current_price <= float(position["sl"]):
            pnl = (current_price - entry) * size_remaining
            actions.append(
                ExitAction(
                    position_id=position["position_id"],
                    kind="SL",
                    price=current_price,
                    size=size_remaining,
                    pnl=pnl,
                )
            )
            self._close_position(position, current_price, "SL", size_remaining, pnl)
            return actions

        # Pyramid scale outs
        scale_pcts = list(self.scale_out_pct)
        # Padding so we always have ≥3 entries to align TP1/TP2/TP3
        while len(scale_pcts) < 3:
            scale_pcts.append(scale_pcts[-1] if scale_pcts else 25.0)

        starting_size = float(position.get("entry_size", 0))
        for tp_label, tp_price, tp_pct in (
            ("TP1", float(position["tp1"]), scale_pcts[0]),
            ("TP2", float(position["tp2"]), scale_pcts[1]),
            ("TP3", float(position["tp3"]), scale_pcts[2]),
        ):
            if tp_label not in remaining:
                continue
            if current_price < tp_price:
                continue
            close_size = starting_size * (tp_pct / 100.0)
            close_size = min(close_size, size_remaining)
            if close_size <= 0:
                continue
            pnl = (tp_price - entry) * close_size
            actions.append(
                ExitAction(
                    position_id=position["position_id"],
                    kind=tp_label,
                    price=tp_price,
                    size=close_size,
                    pnl=pnl,
                )
            )
            size_remaining -= close_size
            remaining.remove(tp_label)
            self.db.log_trade_event(
                position_id=position["position_id"],
                event=f"SCALE_OUT_{tp_label}",
                price=tp_price,
                size=close_size,
                pnl=pnl,
                rule_matched=tp_label,
            )

        if not remaining or size_remaining <= 1e-9:
            self.db.update_position(
                position["position_id"],
                current_price=current_price,
                tp_targets_remaining=remaining,
                status="CLOSED",
            )
            self._fire_close_hook(position)
        elif remaining != position.get("tp_targets_remaining"):
            self.db.update_position(
                position["position_id"],
                current_price=current_price,
                tp_targets_remaining=remaining,
                status="PARTIALLY_CLOSED",
            )
        else:
            self.db.update_position(
                position["position_id"],
                current_price=current_price,
            )
        return actions

    def _close_position(
        self,
        position: Dict[str, Any],
        price: float,
        rule: str,
        size: float,
        pnl: float,
    ) -> None:
        self.db.update_position(
            position["position_id"],
            current_price=price,
            tp_targets_remaining=[],
            status="CLOSED",
        )
        self.db.log_trade_event(
            position_id=position["position_id"],
            event="EXIT",
            price=price,
            size=size,
            pnl=pnl,
            rule_matched=rule,
        )
        logger.info("Closed position %s via %s @ %.2f pnl=%.2f", position["position_id"], rule, price, pnl)
        self._fire_close_hook(position)

    def _fire_close_hook(self, position: Dict[str, Any]) -> None:
        if self.on_close is None:
            return
        # Refresh from DB so we hand the hook the final persisted state, plus events
        try:
            refreshed = self.db.get_position(position["position_id"]) or position
            events = self.db.get_trades_since(0)
            events = [e for e in events if e.get("position_id") == position["position_id"]]
            self.on_close(refreshed, events)
        except Exception as e:  # noqa: BLE001 — close hook must never crash the loop
            logger.exception("close hook failed for %s: %s", position.get("position_id"), e)
