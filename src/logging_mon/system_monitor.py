"""Periodic health checks for data freshness, API, Telegram, disk."""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Optional

from src.data_layer.bybit_connector import BybitConnector
from src.telegram_bot.bot import TelegramBot

logger = logging.getLogger(__name__)


class SystemMonitor:
    def __init__(
        self,
        connector: BybitConnector,
        telegram: TelegramBot,
        log_dir: Path,
        stale_threshold_seconds: int = 300,
    ) -> None:
        self.connector = connector
        self.telegram = telegram
        self.log_dir = Path(log_dir)
        self.stale_threshold = stale_threshold_seconds
        self.last_data_update: Optional[float] = None
        self.last_check = 0.0

    def mark_data_update(self) -> None:
        self.last_data_update = time.time()

    def check(self, interval_seconds: int = 60) -> None:
        now = time.time()
        if (now - self.last_check) < interval_seconds:
            return
        self.last_check = now

        if self.last_data_update and (now - self.last_data_update) > self.stale_threshold:
            self._alert(
                f"DATA STALE: no candle update in {int(now - self.last_data_update)}s"
            )

        try:
            free_gb = shutil.disk_usage(self.log_dir).free / (1024 ** 3)
            if free_gb < 1:
                self._alert(f"LOW DISK: {free_gb:.2f}GB remaining")
        except Exception as e:  # noqa: BLE001
            logger.warning("disk usage check failed: %s", e)

    def _alert(self, msg: str) -> None:
        logger.error("HEALTH ALERT: %s", msg)
        self.telegram.send_message(f"🚨 {msg}", urgent=True)
