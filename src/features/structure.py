"""Lens 2 — Structure.

Swing highs/lows, BOS (break of structure), FVG (fair value gap), and dealing
range. SMC vocabulary, rendered from candles only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StructureFeatures:
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    last_bos: Optional[str] = None  # "bull" | "bear"
    bullish_fvgs: List[Dict[str, Any]] = field(default_factory=list)
    bearish_fvgs: List[Dict[str, Any]] = field(default_factory=list)
    in_premium: Optional[bool] = None
    dealing_range: Optional[Dict[str, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def find_swing_points(
    candles: List[Dict[str, float]],
    lookback: int = 5,
) -> Dict[str, List[Dict[str, Any]]]:
    """Find pivot highs/lows over a 2*lookback+1 window."""
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        c = candles[i]
        window = candles[i - lookback : i + lookback + 1]
        if c["high"] >= max(w["high"] for w in window):
            highs.append({"index": i, "price": c["high"], "ts": c["timestamp"]})
        if c["low"] <= min(w["low"] for w in window):
            lows.append({"index": i, "price": c["low"], "ts": c["timestamp"]})
    return {"highs": highs, "lows": lows}


def detect_fvgs(
    candles: List[Dict[str, float]],
    max_age: int = 50,
) -> Dict[str, List[Dict[str, Any]]]:
    """Three-bar FVG.

    Bullish: candle[n-2].high < candle[n].low — gap pointing up.
    Bearish: candle[n-2].low > candle[n].high — gap pointing down.
    """
    bullish: List[Dict[str, Any]] = []
    bearish: List[Dict[str, Any]] = []
    n = len(candles)
    start = max(2, n - max_age)
    for i in range(start, n):
        a = candles[i - 2]
        c = candles[i]
        if a["high"] < c["low"]:
            bullish.append(
                {"low": a["high"], "high": c["low"], "ts": c["timestamp"], "index": i}
            )
        if a["low"] > c["high"]:
            bearish.append(
                {"low": c["high"], "high": a["low"], "ts": c["timestamp"], "index": i}
            )
    return {"bullish": bullish, "bearish": bearish}


def detect_bos(
    candles: List[Dict[str, float]],
    swings: Dict[str, List[Dict[str, Any]]],
) -> Optional[str]:
    """Has the last close broken the most recent swing high (bull) or low (bear)?"""
    if not candles:
        return None
    last_close = candles[-1]["close"]
    if swings["highs"]:
        if last_close > swings["highs"][-1]["price"]:
            return "bull"
    if swings["lows"]:
        if last_close < swings["lows"][-1]["price"]:
            return "bear"
    return None


def extract(candles: List[Dict[str, float]], lookback: int = 5) -> StructureFeatures:
    if not candles:
        return StructureFeatures()
    swings = find_swing_points(candles, lookback)
    fvgs = detect_fvgs(candles)
    bos = detect_bos(candles, swings)

    dealing_range: Optional[Dict[str, float]] = None
    in_premium: Optional[bool] = None
    if swings["highs"] and swings["lows"]:
        recent_highs = [s["price"] for s in swings["highs"][-3:]]
        recent_lows = [s["price"] for s in swings["lows"][-3:]]
        dh = max(recent_highs)
        dl = min(recent_lows)
        midpoint = (dh + dl) / 2
        dealing_range = {"high": dh, "low": dl, "midpoint": midpoint}
        in_premium = candles[-1]["close"] > midpoint

    return StructureFeatures(
        swing_high=swings["highs"][-1]["price"] if swings["highs"] else None,
        swing_low=swings["lows"][-1]["price"] if swings["lows"] else None,
        last_bos=bos,
        bullish_fvgs=fvgs["bullish"][-5:],
        bearish_fvgs=fvgs["bearish"][-5:],
        in_premium=in_premium,
        dealing_range=dealing_range,
    )
