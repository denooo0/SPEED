"""Tests for the ParquetStore."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.data_layer.historical.storage import ParquetStore


def _ohlcv_frame() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=5, freq="5min", tz="UTC", name="timestamp")
    return pd.DataFrame(
        {
            "open": [1, 2, 3, 4, 5],
            "high": [1.5, 2.5, 3.5, 4.5, 5.5],
            "low": [0.5, 1.5, 2.5, 3.5, 4.5],
            "close": [1.2, 2.2, 3.2, 4.2, 5.2],
            "volume": [10, 20, 30, 40, 50],
        },
        index=idx,
    )


def _series_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03"], utc=True
            ),
            "value": [100.0, 101.5, 102.0],
        }
    )


def test_write_then_read_roundtrip_indexed_frame():
    with tempfile.TemporaryDirectory() as tmp:
        store = ParquetStore(Path(tmp))
        df = _ohlcv_frame()
        path = store.write("bybit/XAUUSD_5m", df)
        assert path.exists()
        assert store.has("bybit/XAUUSD_5m")
        out = store.read("bybit/XAUUSD_5m")
        # Parquet doesn't roundtrip the DatetimeIndex `freq` attribute; compare
        # by value, not by metadata.
        pd.testing.assert_frame_equal(
            out.sort_index(),
            df.sort_index(),
            check_freq=False,
        )


def test_read_range_filters_indexed_frame():
    with tempfile.TemporaryDirectory() as tmp:
        store = ParquetStore(Path(tmp))
        df = _ohlcv_frame()
        store.write("ohlcv", df)
        start = datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 0, 15, tzinfo=timezone.utc)
        out = store.read("ohlcv", start=start, end=end)
        assert len(out) == 3
        assert out.index.min() == pd.Timestamp("2024-01-01 00:05", tz="UTC")
        assert out.index.max() == pd.Timestamp("2024-01-01 00:15", tz="UTC")


def test_read_range_filters_date_column():
    with tempfile.TemporaryDirectory() as tmp:
        store = ParquetStore(Path(tmp))
        df = _series_frame()
        store.write("fred/DGS10", df)
        out = store.read(
            "fred/DGS10",
            start=datetime(2024, 1, 2, tzinfo=timezone.utc),
            end=datetime(2024, 1, 3, tzinfo=timezone.utc),
        )
        assert len(out) == 2
        assert out["value"].tolist() == [101.5, 102.0]


def test_append_mode_dedupes_and_sorts():
    with tempfile.TemporaryDirectory() as tmp:
        store = ParquetStore(Path(tmp))
        df = _ohlcv_frame()
        store.write("ohlcv", df.iloc[:3])
        # Overlap last row of first batch with first row of second batch
        store.write("ohlcv", df.iloc[2:], mode="append")
        out = store.read("ohlcv")
        # Should contain 5 unique rows; deduped on the index.
        assert len(out) == 5
        assert out.index.is_monotonic_increasing


def test_list_returns_nested_names():
    with tempfile.TemporaryDirectory() as tmp:
        store = ParquetStore(Path(tmp))
        store.write("bybit/XAUUSD_5m", _ohlcv_frame())
        store.write("fred/DGS10", _series_frame())
        names = store.list()
        assert "bybit/XAUUSD_5m" in names
        assert "fred/DGS10" in names


def test_read_missing_raises():
    with tempfile.TemporaryDirectory() as tmp:
        store = ParquetStore(Path(tmp))
        with pytest.raises(FileNotFoundError):
            store.read("does_not_exist")


def test_write_none_raises():
    with tempfile.TemporaryDirectory() as tmp:
        store = ParquetStore(Path(tmp))
        with pytest.raises(ValueError):
            store.write("x", None)


def test_has_returns_false_for_missing():
    with tempfile.TemporaryDirectory() as tmp:
        store = ParquetStore(Path(tmp))
        assert store.has("nope") is False
