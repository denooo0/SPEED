"""Lens 1 — Flow.

CVD approximation, volume spikes, large prints. Pure-functional over candles
so it's testable without a live exchange feed. WebSocket-driven CVD will
replace `estimate_aggressor` once the streaming connector lands.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List


@dataclass
class FlowFeatures:
    cvd_full: float = 0.0
    cvd_recent: float = 0.0
    volume_total: float = 0.0
    volume_avg: float = 0.0
    volume_spike: bool = False
    volume_ratio: float = 1.0
    aggressor_bias: str = "neutral"
    last_close_vs_open_bp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def estimate_aggressor(candle: Dict[str, float]) -> int:
    """Approximate aggressor side from candle direction. +1 buy, -1 sell, 0 doji."""
    diff = candle["close"] - candle["open"]
    if diff > 0:
        return 1
    if diff < 0:
        return -1
    return 0


def extract(
    candles: List[Dict[str, float]],
    recent_window: int = 10,
    vol_lookback: int = 20,
) -> FlowFeatures:
    if not candles:
        return FlowFeatures()

    cvd_full = sum(estimate_aggressor(c) * c["volume"] for c in candles)
    recent = candles[-recent_window:] if len(candles) >= recent_window else candles
    cvd_recent = sum(estimate_aggressor(c) * c["volume"] for c in recent)

    vols = [c["volume"] for c in candles]
    avg_window = vols[-vol_lookback:] if len(vols) >= vol_lookback else vols
    avg = sum(avg_window) / len(avg_window) if avg_window else 0.0
    last_vol = vols[-1] if vols else 0.0
    spike = last_vol > avg * 2 if avg > 0 else False
    ratio = (last_vol / avg) if avg > 0 else 1.0

    bias = "buy" if cvd_recent > 0 else "sell" if cvd_recent < 0 else "neutral"

    last = candles[-1]
    open_price = last["open"] or 1.0
    close_vs_open_bp = (last["close"] - last["open"]) / open_price * 10_000

    return FlowFeatures(
        cvd_full=cvd_full,
        cvd_recent=cvd_recent,
        volume_total=sum(vols),
        volume_avg=avg,
        volume_spike=spike,
        volume_ratio=ratio,
        aggressor_bias=bias,
        last_close_vs_open_bp=close_vs_open_bp,
    )
