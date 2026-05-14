"""Tests for the paginated Bybit historical fetcher.

We don't touch the network — we stub the ccxt-like exchange object with a
minimal fake that returns canned candles per `since` cursor.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import List

import pandas as pd
import pytest

from src.data_layer.historical.bybit_historical import (
    BybitHistoricalFetcher,
    PER_REQUEST_LIMIT,
)


BAR_MS = 300_000  # 5m


class _FakeExchange:
    """Minimal ccxt-compatible stub."""

    def __init__(self, start_ms: int, total_bars: int):
        self.start_ms = start_ms
        self.total_bars = total_bars
        self.calls: List[dict] = []

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append(
            {"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit}
        )
        # Return up to `limit` bars starting at the first bar >= since.
        if since is None:
            return []
        # First bar index >= since.
        first_idx = max(0, (since - self.start_ms + BAR_MS - 1) // BAR_MS)
        end_idx = min(self.total_bars, first_idx + (limit or PER_REQUEST_LIMIT))
        rows = []
        for i in range(first_idx, end_idx):
            ts = self.start_ms + i * BAR_MS
            rows.append([ts, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 100 + i])
        return rows


class _FakeConnector:
    def __init__(self, exchange: _FakeExchange):
        self.exchange = exchange


def test_paginates_across_multiple_requests():
    start_ms = 1_700_000_000_000  # arbitrary
    total = PER_REQUEST_LIMIT * 2 + 250  # forces 3 paginated calls
    exch = _FakeExchange(start_ms, total)
    fetcher = BybitHistoricalFetcher(_FakeConnector(exch))

    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end = start + timedelta(milliseconds=BAR_MS * (total - 1))
    df = fetcher.fetch_candles_range("XAUUSD", "5m", start, end)

    assert len(df) == total
    assert df.index.is_monotonic_increasing
    assert df.index.tz is not None
    # First and last timestamps line up.
    assert df.index[0] == pd.Timestamp(start_ms, unit="ms", tz="UTC")
    assert df.index[-1] == pd.Timestamp(
        start_ms + (total - 1) * BAR_MS, unit="ms", tz="UTC"
    )
    # At least 3 pagination calls.
    assert len(exch.calls) >= 3


def test_truncates_to_end_window():
    start_ms = 1_700_000_000_000
    total = 100
    exch = _FakeExchange(start_ms, total)
    fetcher = BybitHistoricalFetcher(_FakeConnector(exch))

    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    # Ask for only the first 10 bars.
    end = start + timedelta(milliseconds=BAR_MS * 9)
    df = fetcher.fetch_candles_range("XAUUSD", "5m", start, end)

    assert len(df) == 10
    assert df.index[-1] == pd.Timestamp(start_ms + 9 * BAR_MS, unit="ms", tz="UTC")


def test_invalid_timeframe_raises():
    fetcher = BybitHistoricalFetcher(_FakeConnector(_FakeExchange(0, 0)))
    with pytest.raises(ValueError):
        fetcher.fetch_candles_range(
            "XAUUSD",
            "7m",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 2, tzinfo=timezone.utc),
        )


def test_inverted_window_raises():
    fetcher = BybitHistoricalFetcher(_FakeConnector(_FakeExchange(0, 0)))
    with pytest.raises(ValueError):
        fetcher.fetch_candles_range(
            "XAUUSD",
            "5m",
            datetime(2024, 1, 2, tzinfo=timezone.utc),
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )


def test_empty_response_returns_empty_frame():
    class _EmptyExch:
        def fetch_ohlcv(self, *_a, **_kw):
            return []

    fetcher = BybitHistoricalFetcher(_FakeConnector(_EmptyExch()))
    df = fetcher.fetch_candles_range(
        "XAUUSD",
        "5m",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    assert df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None


def test_retry_on_transient_failure():
    """Two failures then a success should still return data."""

    class _FlakyExch:
        def __init__(self):
            self.attempt = 0

        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            self.attempt += 1
            if self.attempt < 3:
                raise RuntimeError("transient")
            return [[since, 1.0, 1.1, 0.9, 1.05, 10.0]]

    flaky = _FlakyExch()
    fetcher = BybitHistoricalFetcher(_FakeConnector(flaky))
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    df = fetcher.fetch_candles_range("XAUUSD", "5m", start, end)
    assert flaky.attempt >= 3
    assert len(df) == 1


@pytest.mark.skipif(
    not os.environ.get("BYBIT_API_KEY"),
    reason="Live smoke test requires BYBIT_API_KEY",
)
def test_live_smoke_fetch_last_30_days_m5():
    """Optional smoke test: fetch last 30 days of 5m bars from live Bybit."""
    from src.data_layer.bybit_connector import BybitConnector

    connector = BybitConnector(
        api_key=os.environ["BYBIT_API_KEY"],
        api_secret=os.environ.get("BYBIT_API_SECRET", ""),
        testnet=False,
    )
    fetcher = BybitHistoricalFetcher(connector)
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=30)
    df = fetcher.fetch_candles_range("XAUUSD", "5m", start, end)
    assert not df.empty
    assert df.index.is_monotonic_increasing
    # Sanity: expect at least a few hundred bars over 30 days.
    assert len(df) > 100
