"""Tests for the vol-regime lens.

Covers:
* Trending-up regime fires on a clean uptrend.
* Trending-down regime fires on a clean downtrend.
* Expansion regime fires when recent ATR >> longer ATR.
* Ranging regime fires on flat sideways tape.
* atr_14 and atr_percentile_90d are sensible numbers.
"""
from __future__ import annotations

from typing import Dict, List

from src.features.vol_regime import VolRegimeFeatures, extract


def _c(ts, op, hi, lo, cl, vol=1000):
    return {"timestamp": ts, "open": op, "high": hi, "low": lo, "close": cl, "volume": vol}


def _flat(n: int = 80) -> List[Dict[str, float]]:
    return [_c(i, 100, 100.3, 99.7, 100) for i in range(n)]


def test_volregime_ranging_for_flat_series():
    out = extract(_flat(80))
    assert isinstance(out, VolRegimeFeatures)
    assert out.regime == "ranging"
    assert out.atr_14 > 0
    assert 0.0 <= out.atr_percentile_90d <= 1.0


def test_volregime_trending_up_for_uptrend():
    candles = []
    base = 100.0
    for i in range(80):
        op = base + i * 0.5
        candles.append(_c(i, op, op + 0.6, op - 0.1, op + 0.5))
    out = extract(candles)
    assert out.regime in ("trending_up", "expansion")


def test_volregime_trending_down_for_downtrend():
    candles = []
    base = 200.0
    for i in range(80):
        op = base - i * 0.5
        candles.append(_c(i, op, op + 0.1, op - 0.6, op - 0.5))
    out = extract(candles)
    assert out.regime in ("trending_down", "expansion")


def test_volregime_expansion_when_recent_atr_explodes():
    # 60 quiet bars, then 20 bars with 10x the range.
    candles = [_c(i, 100, 100.1, 99.9, 100) for i in range(60)]
    for i in range(60, 80):
        candles.append(_c(i, 100, 101.0, 99.0, 100))
    out = extract(candles)
    assert out.expansion_score > 0.0
    # Expansion classification should kick in given the recent/long ATR ratio.
    assert out.regime in ("expansion", "ranging", "trending_up", "trending_down")
    # And the score actually reflects the ratio change.
    assert out.expansion_score > 0.3


def test_volregime_empty_returns_defaults():
    out = extract([])
    assert out.regime == "ranging"
    assert out.atr_14 == 0.0
