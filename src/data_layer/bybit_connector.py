"""Bybit market-data connector with retry logic and freshness validation."""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}

RETRY_DELAYS = (1, 2, 4, 8, 16)


class BybitConnector:
    """Fetches OHLCV / balance / ticker data from Bybit via ccxt."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        import ccxt  # local import keeps unit tests light

        options = {"defaultType": "linear"}
        self.exchange = ccxt.bybit(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": options,
            }
        )
        if testnet:
            try:
                self.exchange.set_sandbox_mode(True)
            except Exception:
                logger.warning("Bybit sandbox mode unavailable; continuing on live URL")

    def fetch_candles(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "5m",
        limit: int = 100,
    ) -> Optional[List[Dict[str, float]]]:
        """Fetch OHLCV with retry/backoff. Returns list of dicts or None."""
        last_err: Optional[Exception] = None
        for attempt, delay in enumerate(RETRY_DELAYS):
            try:
                raw = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                if not raw:
                    raise ValueError("Empty OHLCV response")
                candles = [self._candle_to_dict(c) for c in raw]
                if not self._validate_freshness(candles, timeframe):
                    raise ValueError("Stale data")
                return candles
            except Exception as e:  # noqa: BLE001 — surface upstream
                last_err = e
                logger.warning(
                    "fetch_candles %s %s attempt %d failed: %s",
                    symbol,
                    timeframe,
                    attempt + 1,
                    e,
                )
                if attempt < len(RETRY_DELAYS) - 1:
                    time.sleep(delay)
        logger.error("fetch_candles giving up for %s %s: %s", symbol, timeframe, last_err)
        return None

    def fetch_account_balance(self) -> Optional[Dict[str, float]]:
        try:
            balance = self.exchange.fetch_balance()
        except Exception as e:  # noqa: BLE001
            logger.error("fetch_account_balance failed: %s", e)
            return None

        for asset in ("USDT", "USD"):
            if asset in balance.get("total", {}):
                return {
                    "total": float(balance["total"].get(asset) or 0),
                    "free": float(balance["free"].get(asset) or 0),
                    "used": float(balance["used"].get(asset) or 0),
                }
        return {"total": 0.0, "free": 0.0, "used": 0.0}

    def fetch_ticker(self, symbol: str = "XAUUSD") -> Optional[Dict[str, float]]:
        try:
            t = self.exchange.fetch_ticker(symbol)
            return {
                "bid": float(t.get("bid") or 0),
                "ask": float(t.get("ask") or 0),
                "last": float(t.get("last") or 0),
                "timestamp": int(t.get("timestamp") or 0),
            }
        except Exception as e:  # noqa: BLE001
            logger.error("fetch_ticker failed: %s", e)
            return None

    @staticmethod
    def _candle_to_dict(candle: List[float]) -> Dict[str, float]:
        return {
            "timestamp": int(candle[0]),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
        }

    @staticmethod
    def _validate_freshness(candles: List[Dict[str, float]], timeframe: str) -> bool:
        if not candles:
            return False
        latest = candles[-1]["timestamp"]
        now_ms = int(time.time() * 1000)
        # Allow up to 2x the timeframe in age before declaring stale
        slack = TIMEFRAME_MS.get(timeframe, 300_000) * 2
        return (now_ms - latest) <= slack
