"""Telegram notifier with built-in rate limiting."""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    """One-way notifier (signals/alerts) via Telegram HTTP API."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.token = cfg.get("token")
        self.chat_id = cfg.get("chat_id")
        self.enabled = bool(cfg.get("enabled", False))
        self.max_per_minute = int(cfg.get("max_messages_per_min", 5))
        self._sent_at: Deque[float] = deque()
        self._queue: Deque[str] = deque()

    def send_message(self, text: str, urgent: bool = False) -> bool:
        if not self.enabled:
            logger.debug("Telegram disabled — would send: %s", text)
            return False
        if not self._allow(urgent):
            logger.warning("Telegram rate-limited; queueing message")
            self._queue.append(text)
            return False
        return self._post(text)

    def send_signal(self, signal: Dict[str, Any]) -> bool:
        text = self._format_signal(signal)
        return self.send_message(text)

    def flush_queue(self) -> None:
        while self._queue and self._allow(False):
            text = self._queue.popleft()
            self._post(text)

    # -- internals ---------------------------------------------------------
    def _allow(self, urgent: bool) -> bool:
        now = time.time()
        while self._sent_at and now - self._sent_at[0] > 60:
            self._sent_at.popleft()
        if urgent or len(self._sent_at) < self.max_per_minute:
            self._sent_at.append(now)
            return True
        return False

    def _post(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            logger.debug("Telegram credentials missing")
            return False
        url = API_BASE.format(token=self.token, method="sendMessage")
        try:
            r = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            if r.status_code != 200:
                logger.error("Telegram send failed (%s): %s", r.status_code, r.text)
                return False
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("Telegram send raised: %s", e)
            return False

    @staticmethod
    def _format_signal(s: Dict[str, Any]) -> str:
        return (
            "*XAUUSD ENTRY SIGNAL*\n"
            f"Entry: `{s['entry_price']:.2f}`\n"
            f"SL:    `{s['stop_loss']:.2f}`\n"
            f"TP1:   `{s['tp1']:.2f}`\n"
            f"TP2:   `{s['tp2']:.2f}`\n"
            f"TP3:   `{s['tp3']:.2f}`\n"
            f"Size:  `{s['position_size']:.4f}`\n"
            f"Conf:  `{s['confidence']:.2f}`\n"
            f"Why:   {s.get('reason', '')}"
        )
