"""Order-flow features derived from candles.

For backtest replay we do not have per-trade aggressor data, so we approximate
order-flow signals from candle direction × volume (the same trick `flow.py`
uses for CVD). Live mode keeps using the WebSocket path.

Computed features
-----------------
* `cvd_approx`        — signed-volume CVD over the candle window.
* `cvd_divergence`    — Pearson correlation between cumulative CVD and
                        cumulative price change over a rolling window. A value
                        near +1 means CVD tracks price (confirming), near -1
                        means they diverge (warning).
* `aggressor_imbalance_ratio`
                      — buy-aggressor volume / total volume over the window.
                        0.5 = balanced; >0.5 = buy-led; <0.5 = sell-led.
* `large_print_count` — number of candles in the window whose volume exceeds
                        `large_print_multiplier * median(window_volume)`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List

import numpy as np


@dataclass
class OrderFlowFeatures:
    cvd_approx: float = 0.0
    cvd_divergence: float = 0.0
    aggressor_imbalance_ratio: float = 0.5
    large_print_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _candle_sign(candle: Dict[str, float]) -> int:
    diff = candle["close"] - candle["open"]
    if diff > 0:
        return 1
    if diff < 0:
        return -1
    return 0


def extract(
    candles: List[Dict[str, float]],
    window: int = 20,
    large_print_multiplier: float = 2.5,
) -> OrderFlowFeatures:
    """Compute order-flow features over the last `window` candles.

    Returns zeroed defaults on insufficient data.
    """
    if not candles:
        return OrderFlowFeatures()

    recent = candles[-window:] if len(candles) >= window else candles
    if not recent:
        return OrderFlowFeatures()

    signs = np.array([_candle_sign(c) for c in recent], dtype=float)
    vols = np.array([float(c.get("volume", 0.0)) for c in recent], dtype=float)
    closes = np.array([float(c["close"]) for c in recent], dtype=float)
    opens = np.array([float(c["open"]) for c in recent], dtype=float)

    signed_vol = signs * vols
    cvd_approx = float(signed_vol.sum())

    # Buy-aggressor share. Doji (sign=0) contributes to neither side.
    buy_vol = float(vols[signs > 0].sum())
    sell_vol = float(vols[signs < 0].sum())
    total = buy_vol + sell_vol
    aggressor_ratio = (buy_vol / total) if total > 0 else 0.5

    # Divergence: correlation between cumulative CVD and cumulative price-change.
    price_change = closes - opens
    cum_cvd = np.cumsum(signed_vol)
    cum_pc = np.cumsum(price_change)
    if len(cum_cvd) >= 2 and cum_cvd.std() > 0 and cum_pc.std() > 0:
        corr_matrix = np.corrcoef(cum_cvd, cum_pc)
        divergence = float(corr_matrix[0, 1])
    else:
        divergence = 0.0

    # Large-print count vs median volume.
    if vols.size > 0:
        median_vol = float(np.median(vols))
        threshold = median_vol * large_print_multiplier
        large_print_count = int((vols > threshold).sum()) if threshold > 0 else 0
    else:
        large_print_count = 0

    return OrderFlowFeatures(
        cvd_approx=cvd_approx,
        cvd_divergence=divergence,
        aggressor_imbalance_ratio=float(aggressor_ratio),
        large_print_count=large_print_count,
    )
