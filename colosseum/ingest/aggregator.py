"""Multi-timeframe incremental bar aggregation.

Two hard rules:

  1. O(1) per tick per timeframe. No recomputation. At 1 Hz forever, anything
     that rescans a window is a time bomb.

  2. A bar seals exactly once and is then immutable. The forming bar is exposed
     separately as `live` and may NEVER trigger a structural event. This is the
     repaint firewall -- the difference between an honest system and a backtest
     fairy tale.

Volume classification uses the Lee-Ready tick rule: trades on an uptick are
buyer-initiated, downtick seller-initiated, unchanged inherits the last sign.
For spot gold this operates on tick-volume, which is a proxy for activity, not
contract volume. That limitation is real and is stamped on the data, not hidden.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from ..core.clock import floor_ns
from ..core.ringbuf import Ring
from ..core.types import Bar, Quality, Tick

DEFAULT_INTERVALS = (1, 15, 60, 300, 900, 3600)


class _Builder:
    """Single-timeframe bar builder."""
    __slots__ = ("interval_s", "history", "_cur", "_last_sign", "_o", "_h",
                 "_l", "_c", "_v", "_n", "_bv", "_sv", "_open_ns", "_quality")

    def __init__(self, interval_s: int, history: int):
        self.interval_s = interval_s
        self.history: Ring[Bar] = Ring(history)
        self._cur = False
        self._last_sign = 1
        self._o = self._h = self._l = self._c = 0.0
        self._v = 0.0
        self._n = 0
        self._bv = self._sv = 0.0
        self._open_ns = 0
        self._quality = Quality.CLEAN

    def _start(self, t: Tick, open_ns: int) -> None:
        p = t.mid
        self._cur = True
        self._open_ns = open_ns
        self._o = self._h = self._l = self._c = p
        self._v = t.volume
        self._n = 1
        self._bv = t.volume if self._last_sign > 0 else 0.0
        self._sv = t.volume if self._last_sign < 0 else 0.0
        self._quality = t.quality

    def _seal(self) -> Bar:
        b = Bar(
            t_open_ns=self._open_ns,
            t_close_ns=self._open_ns + self.interval_s * 1_000_000_000,
            interval_s=self.interval_s,
            o=self._o, h=self._h, l=self._l, c=self._c,
            volume=self._v, ticks=self._n,
            buy_vol=self._bv, sell_vol=self._sv,
            closed=True, quality=self._quality,
        )
        self.history.push(b)
        self._cur = False
        return b

    def update(self, t: Tick) -> Optional[Bar]:
        """Feed a tick. Returns a Bar iff a bar just SEALED (closed)."""
        p = t.mid
        open_ns = floor_ns(t.t_ns, self.interval_s)
        sealed: Optional[Bar] = None

        if not self._cur:
            self._start(t, open_ns)
            return None

        if open_ns > self._open_ns:
            sealed = self._seal()
            # Gap across empty buckets: emit flat synthetic bars so downstream
            # indexing stays continuous. Marked SYNTHETIC -> never a label.
            missing = (open_ns - sealed.t_open_ns) // (self.interval_s * 1_000_000_000) - 1
            for k in range(1, int(missing) + 1):
                o_ns = sealed.t_open_ns + k * self.interval_s * 1_000_000_000
                c = sealed.c
                self.history.push(Bar(
                    t_open_ns=o_ns,
                    t_close_ns=o_ns + self.interval_s * 1_000_000_000,
                    interval_s=self.interval_s, o=c, h=c, l=c, c=c,
                    volume=0.0, ticks=0, closed=True, quality=Quality.SYNTHETIC))
            self._start(t, open_ns)
            return sealed

        # -- in-bar update, O(1)
        if p > self._h:
            self._h = p
        if p < self._l:
            self._l = p
        if p > self._c:
            self._last_sign = 1
        elif p < self._c:
            self._last_sign = -1
        self._c = p
        self._v += t.volume
        self._n += 1
        if self._last_sign > 0:
            self._bv += t.volume
        else:
            self._sv += t.volume
        if t.quality is not Quality.CLEAN:
            self._quality = t.quality
        return None

    def live(self) -> Optional[Bar]:
        if not self._cur:
            return None
        return Bar(
            t_open_ns=self._open_ns,
            t_close_ns=self._open_ns + self.interval_s * 1_000_000_000,
            interval_s=self.interval_s, o=self._o, h=self._h, l=self._l,
            c=self._c, volume=self._v, ticks=self._n,
            buy_vol=self._bv, sell_vol=self._sv,
            closed=False, quality=self._quality)


class Aggregator:
    """Fans one tick stream into N timeframes.

    Emits (interval_s, sealed_bar) callbacks so the feature engine only does work
    when there is genuinely new closed information -- the natural, correct
    trigger cadence.
    """

    def __init__(self, intervals=DEFAULT_INTERVALS, history: int = 512):
        self.intervals = tuple(sorted(intervals))
        self.builders: Dict[int, _Builder] = {
            i: _Builder(i, history) for i in self.intervals}

    def update(self, t: Tick) -> List[Bar]:
        sealed: List[Bar] = []
        for i in self.intervals:
            b = self.builders[i].update(t)
            if b is not None:
                sealed.append(b)
        return sealed

    def live(self, interval_s: int) -> Optional[Bar]:
        return self.builders[interval_s].live()

    def history(self, interval_s: int) -> Ring[Bar]:
        return self.builders[interval_s].history

    def closed(self, interval_s: int) -> Optional[Bar]:
        h = self.builders[interval_s].history
        return h[-1] if len(h) else None
