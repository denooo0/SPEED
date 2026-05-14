"""Tests for the FREDMacro fetcher with stubbed FRED + yfinance backends."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from src.data_layer.historical.fred_macro import FREDMacro, FREDMacroConfig


class _FakeFredClient:
    def __init__(self, series_values):
        self.series_values = series_values
        self.calls = []

    def get_series(self, series_id, observation_start=None, observation_end=None):
        self.calls.append(
            {
                "series_id": series_id,
                "observation_start": observation_start,
                "observation_end": observation_end,
            }
        )
        idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
        return pd.Series(self.series_values, index=idx)


def test_fred_path_used_when_api_key_present():
    fake = _FakeFredClient([4.0, 4.05, 4.1])
    macro = FREDMacro(
        api_key="fake-key",
        fred_client_factory=lambda key: fake,
    )
    df = macro.fetch_series(
        "DGS10",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 5, tzinfo=timezone.utc),
    )
    assert list(df.columns) == ["date", "value"]
    assert df["value"].tolist() == [4.0, 4.05, 4.1]
    assert len(fake.calls) == 1
    assert fake.calls[0]["series_id"] == "DGS10"
    assert fake.calls[0]["observation_start"] == "2024-01-01"


def test_yfinance_fallback_when_no_api_key(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    def fake_yf(ticker, **kwargs):
        idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
        return pd.DataFrame({"Close": [104.0, 105.0]}, index=idx)

    macro = FREDMacro(yfinance_download=fake_yf)
    df = macro.fetch_series(
        "DTWEXBGS",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 4, tzinfo=timezone.utc),
    )
    assert df["value"].tolist() == [104.0, 105.0]
    assert df["date"].is_monotonic_increasing


def test_yfinance_fallback_handles_multiindex_columns(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    def fake_yf(ticker, **kwargs):
        idx = pd.to_datetime(["2024-02-01", "2024-02-02"])
        cols = pd.MultiIndex.from_tuples([("Close", ticker), ("Volume", ticker)])
        return pd.DataFrame([[10.0, 100], [11.0, 200]], index=idx, columns=cols)

    macro = FREDMacro(yfinance_download=fake_yf)
    df = macro.fetch_series(
        "VIXCLS",
        datetime(2024, 2, 1, tzinfo=timezone.utc),
        datetime(2024, 2, 3, tzinfo=timezone.utc),
    )
    assert df["value"].tolist() == [10.0, 11.0]


def test_fred_failure_falls_back_to_yfinance():
    def broken_factory(_key):
        class _Broken:
            def get_series(self, *_a, **_kw):
                raise RuntimeError("fred down")
        return _Broken()

    def fake_yf(ticker, **kwargs):
        return pd.DataFrame(
            {"Close": [50.0]}, index=pd.to_datetime(["2024-03-01"])
        )

    macro = FREDMacro(
        api_key="present",
        fred_client_factory=broken_factory,
        yfinance_download=fake_yf,
    )
    df = macro.fetch_series(
        "DCOILWTICO",
        datetime(2024, 3, 1, tzinfo=timezone.utc),
        datetime(2024, 3, 2, tzinfo=timezone.utc),
    )
    assert df["value"].tolist() == [50.0]


def test_unknown_series_with_no_fallback_returns_empty(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    # Override the fallback map to be empty so the unknown series has no proxy.
    config = FREDMacroConfig(yf_fallback={})
    macro = FREDMacro(config=config)
    df = macro.fetch_series(
        "DOESNOTEXIST",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    assert df.empty
    assert list(df.columns) == ["date", "value"]


def test_inverted_window_raises():
    macro = FREDMacro(api_key="x", fred_client_factory=lambda _k: _FakeFredClient([]))
    with pytest.raises(ValueError):
        macro.fetch_series(
            "DGS10",
            datetime(2024, 1, 5, tzinfo=timezone.utc),
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )


def test_yfinance_download_failure_returns_empty(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    def boom(*_a, **_kw):
        raise RuntimeError("network down")

    macro = FREDMacro(yfinance_download=boom)
    df = macro.fetch_series(
        "DGS10",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    assert df.empty


def test_fred_environment_variable_picked_up(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "from-env")
    fake = _FakeFredClient([1.0])
    macro = FREDMacro(fred_client_factory=lambda key: fake)
    assert macro.api_key == "from-env"
    # Make the fake return a single-row series.
    fake.series_values = [1.0]
    fake_idx = pd.to_datetime(["2024-01-02"])

    def factory(_key):
        client = _FakeFredClient([1.0])
        client.get_series = lambda *_a, **_kw: pd.Series([1.0], index=fake_idx)
        return client

    macro2 = FREDMacro(fred_client_factory=factory)
    df = macro2.fetch_series(
        "DGS10",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 5, tzinfo=timezone.utc),
    )
    assert df["value"].tolist() == [1.0]
