"""Volatility regime features.

Built on top of `context.atr()`. Adds:
  * `atr_percentile_90d` — percentile rank of current ATR over ~90 trading days
    of bars (derived from bar timeframe).
  * `regime`              — discrete classification:
        - "expansion"      ATR has expanded sharply (recent ATR >> longer ATR)
        - "trending_up"    price up-slope dominant, ATR moderate
        - "trending_down"  price down-slope dominant, ATR moderate
        - "ranging"        flat slope, ATR low
  * `expansion_score`     — recent_ATR / longer_ATR (clipped 0..3 → /3 → 0..1).

Classification thresholds (kept simple; documented here for future tuning):
    expansion_score > 0.55         → "expansion"
    abs(slope_bp_per_bar) < 5      → "ranging"
    slope_bp_per_bar > 0           → "trending_up"
    slope_bp_per_bar < 0           → "trending_down"
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Literal

import numpy as np

from src.features.context import atr, true_range


Regime = Literal["trending_up", "trending_down", "ranging", "expansion"]


@dataclass
class VolRegimeFeatures:
    atr_14: float = 0.0
    atr_percentile_90d: float = 0.0
    regime: Regime = "ranging"
    expansion_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _bars_per_day(timeframe_minutes: int) -> int:
    # 24h markets — 1440 minutes per day.
    if timeframe_minutes <= 0:
        return 288  # default to 5m
    return max(1, 1440 // timeframe_minutes)


def _rolling_atr_series(candles: List[Dict[str, float]], period: int) -> np.ndarray:
    """Compute ATR series with simple-moving-average smoothing."""
    if len(candles) < period + 1:
        return np.array([])
    trs = np.array(
        [true_range(candles[i - 1], candles[i]) for i in range(1, len(candles))],
        dtype=float,
    )
    if trs.size < period:
        return np.array([])
    # Simple rolling mean ATR
    csum = np.cumsum(np.insert(trs, 0, 0.0))
    atr_series = (csum[period:] - csum[:-period]) / period
    return atr_series


def _slope_bp(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    """Linear-regression slope of closes over `lookback` bars, normalized to bp."""
    if len(candles) < lookback:
        return 0.0
    closes = np.array([c["close"] for c in candles[-lookback:]], dtype=float)
    if closes.size < 2 or closes.mean() == 0:
        return 0.0
    x = np.arange(closes.size, dtype=float)
    slope, _ = np.polyfit(x, closes, 1)
    # Slope per bar, expressed as basis points of the mean price
    return float(slope / closes.mean() * 10_000)


def extract(
    candles: List[Dict[str, float]],
    timeframe_minutes: int = 5,
    period: int = 14,
    expansion_recent_window: int = 14,
    expansion_long_window: int = 50,
) -> VolRegimeFeatures:
    """Compute vol regime features from `candles`. Empty/short → zero defaults."""
    if not candles or len(candles) < period + 1:
        return VolRegimeFeatures()

    atr_14 = atr(candles, period=period)

    # ~90 days of bars for percentile.
    history_bars = _bars_per_day(timeframe_minutes) * 90
    atr_series = _rolling_atr_series(candles, period=period)
    if atr_series.size == 0:
        atr_pct = 0.0
    else:
        recent_history = atr_series[-history_bars:]
        current = float(recent_history[-1])
        rank = float((recent_history <= current).sum()) / float(recent_history.size)
        atr_pct = rank

    # Expansion score: recent ATR vs longer ATR.
    recent_atr = atr(candles, period=expansion_recent_window) if len(candles) > expansion_recent_window else atr_14
    long_atr = atr(candles, period=expansion_long_window) if len(candles) > expansion_long_window else recent_atr
    if long_atr and long_atr > 0:
        raw = recent_atr / long_atr
    else:
        raw = 1.0
    expansion_score = float(min(max(raw, 0.0), 3.0) / 3.0)

    # Classification.
    slope_bp = _slope_bp(candles, lookback=min(20, len(candles)))
    if expansion_score > 0.55 and raw > 1.4:
        regime: Regime = "expansion"
    elif abs(slope_bp) < 5:
        regime = "ranging"
    elif slope_bp > 0:
        regime = "trending_up"
    else:
        regime = "trending_down"

    return VolRegimeFeatures(
        atr_14=float(atr_14),
        atr_percentile_90d=float(atr_pct),
        regime=regime,
        expansion_score=expansion_score,
    )
