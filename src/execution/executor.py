"""Orchestrates entry alerts and exit monitoring."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from src.data_layer.bybit_connector import BybitConnector
from src.risk.position_manager import PositionManager
from src.signals.signal_generator import TradeSignal
from src.telegram_bot.bot import TelegramBot

logger = logging.getLogger(__name__)


class Executor:
    """Translates signals into alerts and watches open positions for exits."""

    def __init__(
        self,
        connector: BybitConnector,
        position_mgr: PositionManager,
        telegram: TelegramBot,
        config: Dict[str, Any],
    ) -> None:
        self.connector = connector
        self.position_mgr = position_mgr
        self.telegram = telegram
        self.max_drift_pips = float(config.get("SIGNAL", {}).get("max_price_drift_pips", 50))

    def execute_entry(
        self,
        signal: TradeSignal,
        ma_at_entry: Optional[float] = None,
        ad_at_entry: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        ticker = self.connector.fetch_ticker()
        if ticker and ticker.get("last"):
            current = float(ticker["last"])
            drift_pips = abs(current - signal.entry_price) / signal.entry_price * 10_000
            if drift_pips > self.max_drift_pips:
                logger.warning(
                    "Skipping entry: price drifted %.1fbp from signal", drift_pips
                )
                return None

        position = self.position_mgr.open_from_signal(
            signal.to_dict(),
            ma_at_entry=ma_at_entry,
            ad_at_entry=ad_at_entry,
        )
        self.telegram.send_signal(signal.to_dict())
        return position

    def monitor_exits(self, latest_price: Optional[float] = None) -> None:
        positions = self.position_mgr.get_open_positions()
        if not positions:
            return
        if latest_price is None:
            ticker = self.connector.fetch_ticker()
            latest_price = float(ticker["last"]) if ticker and ticker.get("last") else None
        if latest_price is None:
            logger.warning("No price available for exit monitoring")
            return

        for pos in positions:
            actions = self.position_mgr.evaluate(pos, latest_price)
            for a in actions:
                self.telegram.send_message(
                    f"*Exit {a.kind}* on `{pos['position_id'][:8]}`\n"
                    f"Price: `{a.price:.2f}`  Size: `{a.size:.4f}`  P&L: `{a.pnl:+.2f}`",
                    urgent=(a.kind == "SL"),
                )
