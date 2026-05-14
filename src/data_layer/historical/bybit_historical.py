"""Paginated historical OHLCV download from Bybit via the existing connector.

Bybit's `fetch_ohlcv` returns at most 1000 candles per request. To stretch
back to 2015 we walk forward from `start` in 1000-bar chunks, then stop
once we've crossed `end` or the exchange returns no new rows.

We reuse `BybitConnector.exchange.fetch_ohlcv` directly so we inherit ccxt's
rate-limiter (the connector has `enableRateLimit=True`). We do NOT call the
connector's own `fetch_candles` helper because that one validates freshness
and limits to ~recent bars — wrong shape for archival pulls.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd

from src.data_layer.bybit_connector import BybitConnector, TIMEFRAME_MS

logger = logging.getLogger(__name__)

# Bybit's documented cap. ccxt typically passes this through verbatim.
PER_REQUEST_LIMIT = 1000

# Retry behaviour for transient network failures during long backfills.
RETRY_DELAYS = (1, 2, 4)


class BybitHistoricalFetcher:
    """Paginated OHLCV downloader sitting on top of an existing BybitConnector."""

    def __init__(self, connector: BybitConnector) -> None:
        self.connector = connector

    # -- public ----------------------------------------------------------
    def fetch_candles_range(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Walk forward from `start` to `end` returning all candles.

        Returns a DataFrame indexed by UTC tz-aware timestamps with columns
        open, high, low, close, volume. Empty DataFrame if nothing was
        returned.
        """
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(
                f"Unsupported timeframe {timeframe!r}; expected one of "
                f"{sorted(TIMEFRAME_MS)}"
            )
        start_ms = _to_ms(start)
        end_ms = _to_ms(end)
        if end_ms <= start_ms:
            raise ValueError(f"end ({end}) must be strictly after start ({start})")
        bar_ms = TIMEFRAME_MS[timeframe]

        all_rows: List[List[float]] = []
        cursor = start_ms
        last_seen_ts: Optional[int] = None

        while cursor < end_ms:
            batch = self._fetch_with_retry(symbol, timeframe, cursor, PER_REQUEST_LIMIT)
            if not batch:
                logger.debug(
                    "No data returned at cursor=%s for %s %s; stopping",
                    cursor, symbol, timeframe,
                )
                break
            # Filter anything past `end` and append.
            for row in batch:
                ts = int(row[0])
                if ts > end_ms:
                    break
                if last_seen_ts is not None and ts <= last_seen_ts:
                    continue
                all_rows.append(row)
                last_seen_ts = ts

            last_batch_ts = int(batch[-1][0])
            if last_batch_ts >= end_ms:
                break
            # Advance the cursor past the last bar we got. If the exchange
            # gave us fewer than the requested limit we're caught up.
            next_cursor = last_batch_ts + bar_ms
            if next_cursor <= cursor:
                # Pathological: cursor not advancing. Bail to avoid an
                # infinite loop.
                logger.warning(
                    "Cursor stalled at %s for %s %s; bailing", cursor, symbol, timeframe,
                )
                break
            cursor = next_cursor
            if len(batch) < PER_REQUEST_LIMIT:
                # Exchange returned a partial page → no more data available.
                break

        return _rows_to_frame(all_rows)

    # -- internals -------------------------------------------------------
    def _fetch_with_retry(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        limit: int,
    ) -> List[List[float]]:
        last_err: Optional[Exception] = None
        for attempt, delay in enumerate(RETRY_DELAYS):
            try:
                raw = self.connector.exchange.fetch_ohlcv(
                    symbol, timeframe, since=since_ms, limit=limit,
                )
                return raw or []
            except Exception as e:  # noqa: BLE001 — surface after final retry
                last_err = e
                logger.warning(
                    "historical fetch attempt %d for %s %s since=%s failed: %s",
                    attempt + 1, symbol, timeframe, since_ms, e,
                )
                if attempt < len(RETRY_DELAYS) - 1:
                    time.sleep(delay)
        logger.error(
            "historical fetch giving up for %s %s since=%s: %s",
            symbol, timeframe, since_ms, last_err,
        )
        return []


def _to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _rows_to_frame(rows: List[List[float]]) -> pd.DataFrame:
    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    if not rows:
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        empty.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
        return empty
    df = pd.DataFrame(rows, columns=cols)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df
