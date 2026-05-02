"""Tests for the indicator engine: MA, A/D, volume."""
from __future__ import annotations

from src.indicators.indicator_engine import (
    ADOscillator,
    IndicatorEngine,
    MACalculator,
    VolumeAnalyzer,
)


def test_ma_returns_none_until_period_filled():
    out = MACalculator.calculate([1, 2, 3], period=5)
    assert out == [None, None, None]


def test_ma_average_correct():
    out = MACalculator.calculate([1, 2, 3, 4, 5], period=3)
    assert out[0] is None and out[1] is None
    assert out[2] == 2.0
    assert out[3] == 3.0
    assert out[4] == 4.0


def test_ma_get_latest_short_input():
    assert MACalculator.get_latest([1, 2], period=5) is None
    assert MACalculator.get_latest([1, 2, 3, 4, 5, 6], period=3) == 5.0


def test_ad_clv_doji():
    assert ADOscillator.calculate_clv(100, 100, 100) == 0.0


def test_ad_clv_bullish_close():
    # Close at high → CLV = 1
    assert ADOscillator.calculate_clv(110, 100, 110) == 1.0
    # Close at low → CLV = -1
    assert ADOscillator.calculate_clv(110, 100, 100) == -1.0


def test_ad_extreme_negative_detection():
    ad = ADOscillator()
    # Inject artificial values
    ad.ad_values = [-200_000, -300_000, -400_000]
    assert ad.is_extreme_negative(threshold=-100_000) is True
    ad.ad_values = [-50, -60, -70]
    assert ad.is_extreme_negative(threshold=-100_000) is False


def test_ad_turning_positive():
    ad = ADOscillator()
    ad.ad_values = [-100, -80, -90]   # not turning yet (last lower)
    assert ad.is_turning_positive() is False
    ad.ad_values = [-100, -120, -100]  # was declining, now up
    assert ad.is_turning_positive() is True


def test_volume_spike_threshold():
    v = VolumeAnalyzer(lookback=4)
    for vol in (10, 10, 10, 10):
        v.update(vol)
    v.update(15)
    assert v.is_spike(multiplier=2.0) is False
    v.update(50)
    assert v.is_spike(multiplier=2.0) is True


def test_indicator_engine_seed_idempotent():
    eng = IndicatorEngine(ma_fast=3, ma_slow=5, ad_threshold=-1000, volume_lookback=3)
    candles = []
    for i in range(10):
        candles.append({
            "timestamp": i * 60_000,
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "close": 100 + i,
            "volume": 1000,
        })
    out1 = eng.seed(candles)
    out2 = eng.seed(candles)
    assert out1["ma_fast"] == out2["ma_fast"]
    assert out1["ma_slow"] == out2["ma_slow"]
    assert out1["ad"] == out2["ad"]


def test_indicator_engine_in_place_update():
    eng = IndicatorEngine(ma_fast=3, ma_slow=5, ad_threshold=-1000)
    base = {"timestamp": 60_000, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
    eng.update(base)
    assert len(eng.state.closes) == 1
    # same timestamp → in-place, length should not grow
    eng.update({**base, "close": 100.5})
    assert len(eng.state.closes) == 1
    assert eng.state.closes[-1] == 100.5
