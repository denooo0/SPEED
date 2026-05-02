"""Top-level error handling: log, notify, throttle."""
from __future__ import annotations

import logging
import time
import traceback
from typing import Optional

from src.telegram_bot.bot import TelegramBot

logger = logging.getLogger(__name__)


class ErrorHandler:
    """Logs exceptions and rate-limits Telegram alerts to avoid spam."""

    def __init__(self, telegram: Optional[TelegramBot] = None, alert_cooldown: int = 300) -> None:
        self.telegram = telegram
        self.alert_cooldown = alert_cooldown
        self._last_alert = 0.0

    def handle(self, exc: BaseException, context: str = "") -> None:
        logger.error("Unhandled error in %s: %s", context or "main", exc)
        logger.error("%s", traceback.format_exc())
        now = time.time()
        if self.telegram and (now - self._last_alert) > self.alert_cooldown:
            self._last_alert = now
            self.telegram.send_message(
                f"⚠️ Error in `{context or 'bot'}`: `{type(exc).__name__}: {exc}`",
                urgent=True,
            )
