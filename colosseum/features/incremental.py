"""Streaming estimators. All O(1) per update, all numerically stable.

Naive rolling variance (sum of squares minus square of sum) catastrophically
loses precision on gold's price scale over a full session -- it can go negative.
Welford's algorithm does not. On a system that runs forever, numerical stability
is not pedantry; it's the difference between working in month 1 and working in
month 12.
"""
from __future__ import annotations

import math
from typing import Optional

from ..core.ringbuf import Ring


class EMA:
    __slots__ = ("alpha", "value", "n")

    def __init__(self, period: int):
        self.alpha = 2.0 / (period + 1.0)
        self.value: Optional[float] = None
        self.n = 0

    def update(self, x: float) -> float:
        self.n += 1
        self.value = x if self.value is None else \
            self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class Wilder:
    """Wilder's smoothing (alpha = 1/N). Used by true RSI/ATR -- NOT the same as
    an EMA of period N, a mistake that quietly shifts every threshold."""
    __slots__ = ("period", "value", "n", "_seed")

    def __init__(self, period: int):
        self.period = period
        self.value: Optional[float] = None
        self.n = 0
        self._seed = 0.0

    def update(self, x: float) -> Optional[float]:
        self.n += 1
        if self.n < self.period:
            self._seed += x
            return None
        if self.n == self.period:
            self._seed += x
            self.value = self._seed / self.period
            return self.value
        self.value = (self.value * (self.period - 1) + x) / self.period
        return self.value


class RollingMean:
    __slots__ = ("_r", "_sum")

    def __init__(self, window: int):
        self._r: Ring[float] = Ring(window)
        self._sum = 0.0

    def update(self, x: float) -> float:
        if self._r.full:
            self._sum -= self._r[0]
        self._r.push(x)
        self._sum += x
        return self.value

    @property
    def value(self) -> float:
        n = len(self._r)
        return self._sum / n if n else 0.0

    @property
    def ready(self) -> bool:
        return self._r.full


class Welford:
    """Numerically-stable running mean/variance. Optionally volume-weighted,
    which is exactly what a correct VWAP standard deviation requires."""
    __slots__ = ("w_sum", "mean", "m2")

    def __init__(self) -> None:
        self.w_sum = 0.0
        self.mean = 0.0
        self.m2 = 0.0

    def update(self, x: float, w: float = 1.0) -> None:
        if w <= 0:
            return
        self.w_sum += w
        delta = x - self.mean
        self.mean += (w / self.w_sum) * delta
        self.m2 += w * delta * (x - self.mean)

    @property
    def variance(self) -> float:
        return max(0.0, self.m2 / self.w_sum) if self.w_sum > 0 else 0.0

    @property
    def stdev(self) -> float:
        return math.sqrt(self.variance)

    def reset(self) -> None:
        self.w_sum = self.mean = self.m2 = 0.0


class RSI:
    """Wilder RSI on closed bars only."""
    __slots__ = ("up", "down", "_prev")

    def __init__(self, period: int = 14):
        self.up = Wilder(period)
        self.down = Wilder(period)
        self._prev: Optional[float] = None

    def update(self, close: float) -> Optional[float]:
        if self._prev is None:
            self._prev = close
            return None
        ch = close - self._prev
        self._prev = close
        u = self.up.update(max(ch, 0.0))
        d = self.down.update(max(-ch, 0.0))
        if u is None or d is None:
            return None
        if d == 0:
            return 100.0
        rs = u / d
        return 100.0 - (100.0 / (1.0 + rs))


class ATR:
    __slots__ = ("w", "_prev_close")

    def __init__(self, period: int = 14):
        self.w = Wilder(period)
        self._prev_close: Optional[float] = None

    def update(self, h: float, l: float, c: float) -> Optional[float]:
        if self._prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - self._prev_close), abs(l - self._prev_close))
        self._prev_close = c
        return self.w.update(tr)


class Slope:
    """Least-squares slope over a rolling window. Used for ADL trend -- the
    'is smart money accumulating' coordinate. Sign matters more than magnitude."""
    __slots__ = ("_r",)

    def __init__(self, window: int = 20):
        self._r: Ring[float] = Ring(window)

    def update(self, y: float) -> float:
        self._r.push(y)
        n = len(self._r)
        if n < 3:
            return 0.0
        xm = (n - 1) / 2.0
        ym = sum(self._r) / n
        num = den = 0.0
        for i in range(n):
            dx = i - xm
            num += dx * (self._r[i] - ym)
            den += dx * dx
        return num / den if den else 0.0
