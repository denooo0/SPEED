"""Lightweight validation utilities for OHLCV data."""
from __future__ import annotations

import logging
import time
from typing import Iterable, List, Mapping

logger = logging.getLogger(__name__)


def validate_candles(candles: List[Mapping[str, float]]) -> bool:
    """Return True if every candle has expected keys and sane values."""
    required = ("timestamp", "open", "high", "low", "close", "volume")
    if not candles:
        return False
    for c in candles:
        if any(k not in c for k in required):
            return False
        if c["high"] < c["low"]:
            return False
        if c["volume"] < 0:
            return False
    return True


def is_data_fresh(candles: List[Mapping[str, float]], max_age_seconds: int = 300) -> bool:
    if not candles:
        return False
    latest_ms = candles[-1]["timestamp"]
    age = (time.time() * 1000 - latest_ms) / 1000.0
    if age > max_age_seconds:
        logger.warning("Data is %.0fs old (limit %ds)", age, max_age_seconds)
        return False
    return True


def candle_close_prices(candles: Iterable[Mapping[str, float]]) -> List[float]:
    return [float(c["close"]) for c in candles]
