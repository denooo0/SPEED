"""Tests for spike / consolidation / bounce detection."""
from __future__ import annotations

from src.patterns.spike_detector import SpikeDetector, price_diff_pips


def _candle(ts, open_, high, low, close, vol=1000):
    return {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": vol}


def test_price_diff_pips_basic():
    # delta is normalised by the second argument; 100 vs 99 → ≈101bp (1/99 * 10000)
    assert abs(price_diff_pips(100, 99) - 10_000 / 99) < 1e-6
    # Symmetric magnitude regardless of order
    assert price_diff_pips(99, 100) == 100.0


def test_no_spike_without_drop():
    det = SpikeDetector(min_spike_pips=80, min_spike_candles=5)
    candles = [_candle(i, 2000, 2001, 1999, 2000) for i in range(5)]
    out = det.update(candles, {"ad_extreme": True})
    assert out["spike"] is False


def test_spike_requires_ad_extreme():
    det = SpikeDetector(min_spike_pips=80, min_spike_candles=5)
    # 100 pip drop top→bottom of last 5 candles
    candles = [
        _candle(0, 2000, 2001, 1999, 2000),
        _candle(1, 2000, 2002, 1998, 1995),
        _candle(2, 1995, 1996, 1990, 1990),
        _candle(3, 1990, 1991, 1985, 1985),
        _candle(4, 1985, 1986, 1980, 1981),
    ]
    out = det.update(candles, {"ad_extreme": False})
    assert out["spike"] is False
    out = det.update(candles, {"ad_extreme": True, "ad": -100_000})
    assert out["spike"] is True
    assert det.state.spike_price == 1980


def test_consolidation_after_spike():
    det = SpikeDetector(
        min_spike_pips=80,
        min_spike_candles=5,
        consolidation_candles=5,
        consolidation_max_range_pips=20,
    )
    spike_candles = [
        _candle(0, 2000, 2001, 1999, 2000),
        _candle(1, 2000, 2002, 1998, 1995),
        _candle(2, 1995, 1996, 1990, 1990),
        _candle(3, 1990, 1991, 1985, 1985),
        _candle(4, 1985, 1986, 1980, 1981),
    ]
    det.update(spike_candles, {"ad_extreme": True, "ad": -100_000})
    assert det.state.detected
    consolidation = spike_candles + [
        _candle(5, 1981, 1982, 1980, 1981),
        _candle(6, 1981, 1982, 1980, 1981),
        _candle(7, 1981, 1982, 1980, 1981),
        _candle(8, 1981, 1982, 1980, 1981),
        _candle(9, 1981, 1982, 1980, 1981),
    ]
    assert det.is_consolidating(consolidation) is True


def test_bounce_requires_ad_turn_and_above_ma():
    det = SpikeDetector(
        min_spike_pips=80,
        min_spike_candles=5,
        consolidation_candles=5,
        consolidation_max_range_pips=20,
    )
    spike_candles = [
        _candle(0, 2000, 2001, 1999, 2000),
        _candle(1, 2000, 2002, 1998, 1995),
        _candle(2, 1995, 1996, 1990, 1990),
        _candle(3, 1990, 1991, 1985, 1985),
        _candle(4, 1985, 1986, 1980, 1981),
    ]
    det.update(spike_candles, {"ad_extreme": True, "ad": -100_000})

    history = spike_candles + [
        _candle(5, 1981, 1982, 1980, 1981),
        _candle(6, 1981, 1982, 1980, 1981),
        _candle(7, 1981, 1982, 1980, 1981),
        _candle(8, 1981, 1982, 1980, 1981),
        _candle(9, 1981, 1983, 1981, 1983),  # close above MA, range stays narrow
    ]
    # AD not turning → no bounce
    assert det.is_bouncing(history, {"ad_turning_up": False, "ma_fast": 1980}) is False
    # AD turns up but close below MA → no bounce
    assert det.is_bouncing(history, {"ad_turning_up": True, "ma_fast": 2000}) is False
    # Both conditions met → bounce
    assert det.is_bouncing(history, {"ad_turning_up": True, "ma_fast": 1981}) is True
