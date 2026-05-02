"""Real-time indicator engine: MA, A/D oscillator, volume analysis."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class MACalculator:
    """Simple moving average."""

    @staticmethod
    def calculate(prices: List[float], period: int) -> List[Optional[float]]:
        if period <= 0:
            raise ValueError("period must be positive")
        out: List[Optional[float]] = []
        running = 0.0
        for i, p in enumerate(prices):
            running += p
            if i >= period:
                running -= prices[i - period]
            if i + 1 >= period:
                out.append(running / period)
            else:
                out.append(None)
        return out

    @staticmethod
    def get_latest(prices: List[float], period: int) -> Optional[float]:
        if len(prices) < period:
            return None
        window = prices[-period:]
        return sum(window) / period


class ADOscillator:
    """Cumulative Accumulation/Distribution line."""

    def __init__(self) -> None:
        self.ad_values: List[float] = []

    @staticmethod
    def calculate_clv(high: float, low: float, close: float) -> float:
        if high == low:
            return 0.0
        return ((close - low) - (high - close)) / (high - low)

    def update(self, high: float, low: float, close: float, volume: float) -> float:
        clv = self.calculate_clv(high, low, close)
        flow = clv * volume
        cum = flow if not self.ad_values else self.ad_values[-1] + flow
        self.ad_values.append(cum)
        return cum

    def get_latest(self) -> Optional[float]:
        return self.ad_values[-1] if self.ad_values else None

    def is_extreme_negative(self, threshold: float = -50_000) -> bool:
        if len(self.ad_values) < 3:
            return False
        return all(v < threshold for v in self.ad_values[-3:])

    def is_turning_positive(self) -> bool:
        if len(self.ad_values) < 3:
            return False
        a, b, c = self.ad_values[-3], self.ad_values[-2], self.ad_values[-1]
        return b < a and c > b

    def is_improving(self, lookback: int = 3) -> bool:
        if len(self.ad_values) < lookback + 1:
            return False
        recent = self.ad_values[-lookback:]
        return all(recent[i] < recent[i + 1] for i in range(len(recent) - 1))


class VolumeAnalyzer:
    """Volume spike detection over a rolling lookback."""

    def __init__(self, lookback: int = 20) -> None:
        self.lookback = lookback
        self.volumes: List[float] = []

    def update(self, volume: float) -> None:
        self.volumes.append(float(volume))

    def average(self) -> float:
        if not self.volumes:
            return 0.0
        window = self.volumes[-self.lookback :] if len(self.volumes) >= self.lookback else self.volumes
        return sum(window) / len(window)

    def is_spike(self, multiplier: float = 2.0) -> bool:
        if len(self.volumes) < max(self.lookback, 1):
            return False
        return self.volumes[-1] > self.average() * multiplier

    def spike_ratio(self) -> float:
        if not self.volumes:
            return 1.0
        avg = self.average()
        return self.volumes[-1] / avg if avg > 0 else 1.0


@dataclass
class IndicatorState:
    closes: List[float] = field(default_factory=list)
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    last_timestamp: int = 0


class IndicatorEngine:
    """Aggregates per-timeframe indicator state."""

    def __init__(
        self,
        ma_fast: int = 50,
        ma_slow: int = 100,
        ad_threshold: float = -50_000,
        volume_lookback: int = 20,
        volume_multiplier: float = 2.0,
    ) -> None:
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.ad_threshold = ad_threshold
        self.volume_multiplier = volume_multiplier

        self.ad = ADOscillator()
        self.volume = VolumeAnalyzer(lookback=volume_lookback)
        self.state = IndicatorState()
        self._prev_ma_fast: Optional[float] = None
        self._prev_ma_slow: Optional[float] = None

    def reset(self) -> None:
        self.ad = ADOscillator()
        self.volume = VolumeAnalyzer(lookback=self.volume.lookback)
        self.state = IndicatorState()
        self._prev_ma_fast = None
        self._prev_ma_slow = None

    def seed(self, candles: List[Dict[str, float]]) -> Dict[str, Any]:
        """Replay a list of candles to build full history."""
        self.reset()
        latest: Dict[str, Any] = {}
        for c in candles:
            latest = self.update(c)
        return latest

    def update(self, candle: Dict[str, float]) -> Dict[str, Any]:
        ts = int(candle["timestamp"])
        if ts == self.state.last_timestamp and self.state.closes:
            # Update last candle in-place (current bar refresh)
            self.state.closes[-1] = float(candle["close"])
            self.state.highs[-1] = float(candle["high"])
            self.state.lows[-1] = float(candle["low"])
            if self.ad.ad_values:
                self.ad.ad_values.pop()
            if self.volume.volumes:
                self.volume.volumes.pop()
        else:
            self.state.closes.append(float(candle["close"]))
            self.state.highs.append(float(candle["high"]))
            self.state.lows.append(float(candle["low"]))
            self.state.last_timestamp = ts

        self.ad.update(candle["high"], candle["low"], candle["close"], candle["volume"])
        self.volume.update(candle["volume"])

        ma_fast = MACalculator.get_latest(self.state.closes, self.ma_fast)
        ma_slow = MACalculator.get_latest(self.state.closes, self.ma_slow)

        out = {
            "timestamp": ts,
            "ma_fast": ma_fast,
            "ma_slow": ma_slow,
            "ma_fast_prev": self._prev_ma_fast,
            "ma_slow_prev": self._prev_ma_slow,
            "ma_fast_rising": _is_rising(self._prev_ma_fast, ma_fast),
            "ma_slow_rising": _is_rising(self._prev_ma_slow, ma_slow),
            "ad": self.ad.get_latest(),
            "ad_extreme": self.ad.is_extreme_negative(self.ad_threshold),
            "ad_turning_up": self.ad.is_turning_positive(),
            "ad_improving": self.ad.is_improving(),
            "volume_spike": self.volume.is_spike(self.volume_multiplier),
            "volume_ratio": self.volume.spike_ratio(),
        }
        self._prev_ma_fast = ma_fast
        self._prev_ma_slow = ma_slow
        return out


def _is_rising(prev: Optional[float], curr: Optional[float]) -> Optional[bool]:
    if prev is None or curr is None:
        return None
    return curr > prev
