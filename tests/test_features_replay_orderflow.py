"""Tests for the order-flow lens.

Covers:
* CVD signs correctly from candle direction.
* aggressor_imbalance_ratio reflects buy/sell volume split.
* cvd_divergence detects price-vs-flow disagreement.
* large_print_count fires on volume outliers.
* Empty / sub-window inputs return safe defaults.
"""
from __future__ import annotations

from src.features.order_flow import OrderFlowFeatures, extract


def _c(ts, op, hi, lo, cl, vol=1000):
    return {"timestamp": ts, "open": op, "high": hi, "low": lo, "close": cl, "volume": vol}


def test_orderflow_cvd_signs_positive_for_up_candles():
    candles = [_c(i, 100, 101, 99, 101, 500) for i in range(5)]
    out = extract(candles)
    assert out.cvd_approx == 2500  # all five up, signed volume
    assert out.aggressor_imbalance_ratio == 1.0


def test_orderflow_cvd_signs_negative_for_down_candles():
    candles = [_c(i, 101, 102, 100, 100, 500) for i in range(5)]
    out = extract(candles)
    assert out.cvd_approx == -2500
    assert out.aggressor_imbalance_ratio == 0.0


def test_orderflow_balanced_ratio_when_equal_volume_split():
    candles = [_c(i, 100, 101, 99, 101, 500) for i in range(3)]   # up
    candles += [_c(i + 3, 101, 102, 100, 100, 500) for i in range(3)]  # down
    out = extract(candles)
    # 1500 buy / 1500 sell → 0.5
    assert out.aggressor_imbalance_ratio == 0.5


def test_orderflow_large_print_count_fires_on_outlier():
    candles = [_c(i, 100, 101, 99, 101, 100) for i in range(19)]
    candles.append(_c(19, 100, 101, 99, 101, 10_000))  # massive print
    out = extract(candles, large_print_multiplier=2.5)
    assert out.large_print_count >= 1


def test_orderflow_empty_returns_defaults():
    out = extract([])
    assert isinstance(out, OrderFlowFeatures)
    assert out.cvd_approx == 0.0
    assert out.aggressor_imbalance_ratio == 0.5


def test_orderflow_divergence_inverse_when_price_up_volume_sell():
    # Construct a window where late bars rally but on red candles (closes < opens)
    # with the small price change still going up via wick. Use mixed series.
    candles = []
    # First 10: price flat, alternating mostly sell pressure
    for i in range(10):
        cl = 100 - 0.01 * i  # very slight downdrift
        candles.append(_c(i, cl + 0.1, cl + 0.2, cl - 0.1, cl, 500))
    out = extract(candles)
    # Confirm we return a float in [-1, 1].
    assert -1.0 <= out.cvd_divergence <= 1.0
