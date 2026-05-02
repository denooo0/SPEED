"""Lens 3 — Context.

Session, day-of-week, ATR, ATR percentile within recent history. Calendar
windows are stubbed for now — wire in once a feed is selected.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class ContextFeatures:
    session: str = "off-hours"
    day_of_week: str = ""
    hour_utc: int = 0
    atr: float = 0.0
    atr_percentile_recent: Optional[float] = None
    is_news_window: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


SESSIONS = (
    ("asia", 0, 9),
    ("london", 7, 16),
    ("ny", 13, 22),
)


def session_for_hour(hour_utc: int) -> str:
    overlaps = [name for name, start, end in SESSIONS if start <= hour_utc < end]
    if "london" in overlaps and "ny" in overlaps:
        return "ny-overlap"
    if "london" in overlaps:
        return "london"
    if overlaps:
        return overlaps[0]
    return "off-hours"


def true_range(prev: Dict[str, float], curr: Dict[str, float]) -> float:
    return max(
        curr["high"] - curr["low"],
        abs(curr["high"] - prev["close"]),
        abs(curr["low"] - prev["close"]),
    )


def atr(candles: List[Dict[str, float]], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    window = candles[-(period + 1) :]
    trs = [true_range(window[i - 1], window[i]) for i in range(1, len(window))]
    return sum(trs) / len(trs)


def atr_percentile(
    candles: List[Dict[str, float]],
    period: int = 14,
    history_bars: int = 500,
) -> Optional[float]:
    """Percentile rank of the current ATR within the last `history_bars` ATR readings."""
    if len(candles) < period + 2:
        return None
    history: List[float] = []
    for i in range(period, len(candles)):
        window = candles[max(0, i - period) : i + 1]
        if len(window) < period + 1:
            continue
        trs = [true_range(window[j - 1], window[j]) for j in range(1, len(window))]
        history.append(sum(trs) / period)
    if not history:
        return None
    history = history[-history_bars:]
    current = history[-1]
    rank = sum(1 for v in history if v <= current)
    return rank / len(history)


def extract(
    candles: List[Dict[str, float]],
    now: Optional[datetime] = None,
) -> ContextFeatures:
    if not candles:
        return ContextFeatures()
    if now is None:
        last_ts = candles[-1]["timestamp"]
        now = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
    return ContextFeatures(
        session=session_for_hour(now.hour),
        day_of_week=now.strftime("%A"),
        hour_utc=now.hour,
        atr=atr(candles),
        atr_percentile_recent=atr_percentile(candles),
        is_news_window=False,
    )
