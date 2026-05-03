"""Lens 1 — Flow.

CVD, volume spikes, large prints, sweeps. Two data paths:
  - WebSocket buffer (when available): true aggressor-side CVD + sweep +
    large-print detection from per-trade data. Source of truth.
  - Candles (fallback): CVD approximated from candle direction × volume.
    Used during paper testing before the WS is wired, or as a degraded
    fallback if the WS connection drops.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


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
    # WS-only fields — populated when a WSBuffer is supplied
    source: str = "candles"           # "candles" | "ws" | "ws+candles"
    ob_imbalance: Optional[float] = None
    ob_spread: Optional[float] = None
    sweeps_recent_count: int = 0
    large_prints_recent_count: int = 0

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
    ws_snapshot: Optional[Dict[str, Any]] = None,
) -> FlowFeatures:
    """Build FlowFeatures.

    `ws_snapshot` is the dict returned by `WSBuffer.snapshot()`. When supplied
    and recent (book_age_ms <= 5000), CVD and order-book metrics come from
    the WS path; volume spike + close-vs-open still derive from candles.
    """
    if not candles:
        if ws_snapshot:
            return _from_ws_only(ws_snapshot)
        return FlowFeatures()

    # Candle-derived baseline
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

    out = FlowFeatures(
        cvd_full=cvd_full,
        cvd_recent=cvd_recent,
        volume_total=sum(vols),
        volume_avg=avg,
        volume_spike=spike,
        volume_ratio=ratio,
        aggressor_bias=bias,
        last_close_vs_open_bp=close_vs_open_bp,
        source="candles",
    )
    # Overlay WS-derived metrics when a fresh snapshot is available
    if ws_snapshot and _ws_is_fresh(ws_snapshot):
        out.cvd_full = float(ws_snapshot.get("cvd_full") or out.cvd_full)
        out.cvd_recent = float(ws_snapshot.get("cvd_recent_1m") or out.cvd_recent)
        out.aggressor_bias = (
            "buy" if out.cvd_recent > 0 else "sell" if out.cvd_recent < 0 else "neutral"
        )
        out.ob_imbalance = ws_snapshot.get("ob_imbalance")
        out.ob_spread = ws_snapshot.get("ob_spread")
        out.sweeps_recent_count = len(ws_snapshot.get("sweeps_recent") or [])
        out.large_prints_recent_count = len(ws_snapshot.get("large_prints_recent") or [])
        out.source = "ws+candles"
    return out


def _ws_is_fresh(snapshot: Dict[str, Any], max_age_ms: int = 5000) -> bool:
    age = snapshot.get("ob_age_ms")
    if age is None:
        # Trades-only snapshot with no book yet — still useful
        return bool(snapshot.get("trades_in_buffer"))
    return age <= max_age_ms


def _from_ws_only(snapshot: Dict[str, Any]) -> FlowFeatures:
    cvd_recent = float(snapshot.get("cvd_recent_1m") or 0)
    return FlowFeatures(
        cvd_full=float(snapshot.get("cvd_full") or 0),
        cvd_recent=cvd_recent,
        aggressor_bias="buy" if cvd_recent > 0 else "sell" if cvd_recent < 0 else "neutral",
        ob_imbalance=snapshot.get("ob_imbalance"),
        ob_spread=snapshot.get("ob_spread"),
        sweeps_recent_count=len(snapshot.get("sweeps_recent") or []),
        large_prints_recent_count=len(snapshot.get("large_prints_recent") or []),
        source="ws",
    )
