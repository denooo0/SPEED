"""Tests for the correlation lens.

Covers:
* Returns None values and 'unknown' regime when macro_series is absent.
* Returns numeric correlations when macro_series is supplied.
* Classification reaches 'haven_bid' when gold inversely tracks DXY & SPX.
* Correlation values are bounded in [-1, 1].
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.correlation import CorrelationFeatures, extract


def _daily(n: int, start_price: float, daily_drift: float, noise: float, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="1D", tz="UTC")
    rets = rng.normal(daily_drift, noise, size=n)
    closes = start_price + np.cumsum(rets)
    return pd.DataFrame({"close": closes}, index=idx)


def test_correlation_returns_none_when_macro_absent():
    xau = _daily(120, 2000.0, 0.1, 1.0)
    out = extract(xau, macro_series=None)
    assert isinstance(out, CorrelationFeatures)
    assert out.corr_dxy_30d is None
    assert out.corr_us10y_30d is None
    assert out.corr_spx_30d is None
    assert out.regime_classification == "unknown"


def test_correlation_returns_none_when_macro_dict_empty():
    xau = _daily(120, 2000.0, 0.1, 1.0)
    out = extract(xau, macro_series={})
    assert out.corr_dxy_30d is None
    assert out.regime_classification == "unknown"


def test_correlation_haven_bid_when_xau_inverse_to_dxy_and_spx():
    n = 180
    rng = np.random.default_rng(42)
    base_rets = rng.normal(0, 1.0, size=n)
    idx = pd.date_range("2025-01-01", periods=n, freq="1D", tz="UTC")

    # XAU and (DXY, SPX) inversely correlated by construction.
    xau_close = 2000 + np.cumsum(base_rets)
    dxy_close = 100 - np.cumsum(base_rets) * 0.1
    spx_close = 4500 - np.cumsum(base_rets) * 5

    xau = pd.DataFrame({"close": xau_close}, index=idx)
    macro = {
        "DXY": pd.DataFrame({"close": dxy_close}, index=idx),
        "SPX": pd.DataFrame({"close": spx_close}, index=idx),
    }
    out = extract(xau, macro_series=macro, window_days=30)

    assert out.corr_dxy_30d is not None and out.corr_dxy_30d < -0.5
    assert out.corr_spx_30d is not None and out.corr_spx_30d < -0.5
    assert out.regime_classification == "haven_bid"


def test_correlation_values_bounded():
    n = 120
    rng = np.random.default_rng(0)
    idx = pd.date_range("2025-01-01", periods=n, freq="1D", tz="UTC")
    xau = pd.DataFrame({"close": 2000 + rng.normal(0, 1, n).cumsum()}, index=idx)
    macro = {"DXY": pd.DataFrame({"close": 100 + rng.normal(0, 0.3, n).cumsum()}, index=idx)}
    out = extract(xau, macro_series=macro)
    if out.corr_dxy_30d is not None:
        assert -1.0 <= out.corr_dxy_30d <= 1.0


def test_correlation_returns_none_when_macro_too_short():
    xau = _daily(120, 2000.0, 0.1, 1.0)
    # Only 3 days of DXY → way below the rolling-window minimum.
    short = pd.DataFrame(
        {"close": [100.0, 100.1, 100.2]},
        index=pd.date_range("2025-04-01", periods=3, freq="1D", tz="UTC"),
    )
    out = extract(xau, macro_series={"DXY": short})
    assert out.corr_dxy_30d is None
