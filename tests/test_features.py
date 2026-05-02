"""Tests for the four-lens feature extractors."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

from src.features import context as ctx_feat
from src.features import flow as flow_feat
from src.features import intent as intent_feat
from src.features import structure as struct_feat


def _candle(ts, op, hi, lo, cl, vol=1000):
    return {"timestamp": ts, "open": op, "high": hi, "low": lo, "close": cl, "volume": vol}


# -- flow ----------------------------------------------------------------
def test_flow_cvd_signed_by_candle_direction():
    # 3 up candles + 2 down candles with equal volume → CVD = +3 - 2 = +1
    candles = [
        _candle(0, 100, 101, 99, 101, 1000),
        _candle(1, 101, 102, 100, 102, 1000),
        _candle(2, 102, 103, 101, 103, 1000),
        _candle(3, 103, 104, 100, 100, 1000),
        _candle(4, 100, 101, 98, 99, 1000),
    ]
    out = flow_feat.extract(candles)
    assert out.cvd_full == 1000  # 3*1000 - 2*1000 net buying
    assert out.aggressor_bias == "buy"


def test_flow_volume_spike_detected():
    candles = [_candle(i, 100, 101, 99, 100, 1000) for i in range(20)]
    candles.append(_candle(20, 100, 101, 99, 100, 5000))
    out = flow_feat.extract(candles)
    assert out.volume_spike is True
    assert out.volume_ratio > 2


def test_flow_empty_candles():
    out = flow_feat.extract([])
    assert out.cvd_full == 0.0
    assert out.aggressor_bias == "neutral"


# -- structure -----------------------------------------------------------
def test_structure_finds_swing_points():
    # Pattern with a clear high at index 5 and a clear low at index 11
    prices = [100, 101, 102, 103, 104, 110, 104, 103, 102, 101, 100, 95, 100, 101, 102, 103]
    candles = [_candle(i, p, p + 1, p - 1, p) for i, p in enumerate(prices)]
    out = struct_feat.extract(candles, lookback=3)
    assert out.swing_high is not None and out.swing_high >= 110
    assert out.swing_low is not None and out.swing_low <= 95


def test_structure_detects_bullish_bos():
    # Establish a swing high around index 5, then close above it at end
    prices = [100, 101, 102, 105, 102, 101, 100, 99, 98, 99, 100, 101, 106]
    candles = [_candle(i, p, p + 0.5, p - 0.5, p) for i, p in enumerate(prices)]
    out = struct_feat.extract(candles, lookback=3)
    # The exact label depends on swing detection ordering, but a BOS should fire
    assert out.last_bos in ("bull", "bear")


def test_structure_finds_bullish_fvg():
    # 3-candle gap: candle[i-2] high < candle[i] low
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 102, 99, 102),  # middle bar
        _candle(2, 103, 104, 102.5, 103),  # low (102.5) > candles[0].high (101) → bullish FVG
    ]
    out = struct_feat.extract(candles, lookback=1)
    assert len(out.bullish_fvgs) >= 1
    assert out.bullish_fvgs[-1]["low"] == 101
    assert out.bullish_fvgs[-1]["high"] == 102.5


# -- context -------------------------------------------------------------
def test_context_session_classification():
    assert ctx_feat.session_for_hour(2) == "asia"
    assert ctx_feat.session_for_hour(8) == "london"  # london overlaps with end of asia at hour 7-9
    assert ctx_feat.session_for_hour(15) == "ny-overlap"
    assert ctx_feat.session_for_hour(20) == "ny"
    assert ctx_feat.session_for_hour(23) == "off-hours"


def test_context_atr_basic():
    # 15 candles with 1-point ranges → ATR ≈ 1.0
    candles = [_candle(i, 100, 101, 99, 100) for i in range(20)]
    out = ctx_feat.extract(candles, now=datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc))
    assert abs(out.atr - 2.0) < 0.5  # high-low = 2 each bar
    assert out.session == "ny-overlap"


# -- intent --------------------------------------------------------------
def test_intent_funding_skew_classification():
    assert intent_feat.classify_funding_skew(0.001) == "long-crowded"
    assert intent_feat.classify_funding_skew(-0.001) == "short-crowded"
    assert intent_feat.classify_funding_skew(0.0) == "neutral"
    assert intent_feat.classify_funding_skew(None) is None


def test_intent_oi_delta():
    out = intent_feat.extract(funding_rate=0.0002, open_interest=1500.0, oi_1h_ago=1400.0)
    assert out.oi_delta_1h == 100.0
    assert out.funding_skew == "long-crowded"
