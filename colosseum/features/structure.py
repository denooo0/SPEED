"""Market structure state machine: trend, BOS, CHoCH.

Definitions (kept strict, because loose definitions are how "structure" becomes
astrology):

    Uptrend      : sequence of higher highs AND higher lows
    Downtrend    : sequence of lower highs AND lower lows
    BOS (break of structure) : price closes beyond the last swing IN the
                   direction of trend -> continuation
    CHoCH (change of character) : price closes beyond the last swing AGAINST
                   the trend -> the first structural evidence of reversal

All of it computed from CONFIRMED pivots and CLOSED bars only.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..core.types import Bar, Pivot, PivotKind, StructureEvent


class StructureEngine:
    __slots__ = ("trend", "_last_high", "_last_low", "_prev_high", "_prev_low",
                 "events", "_max_events")

    def __init__(self, max_events: int = 32):
        self.trend = "unknown"
        self._last_high: Optional[Pivot] = None
        self._last_low: Optional[Pivot] = None
        self._prev_high: Optional[Pivot] = None
        self._prev_low: Optional[Pivot] = None
        self.events: List[Tuple[int, StructureEvent]] = []
        self._max_events = max_events

    def on_pivot(self, p: Pivot) -> None:
        if p.kind is PivotKind.HIGH:
            self._prev_high, self._last_high = self._last_high, p
        else:
            self._prev_low, self._last_low = self._last_low, p
        self._reassess()

    def _reassess(self) -> None:
        lh, ll, ph, pl = self._last_high, self._last_low, self._prev_high, self._prev_low
        if not (lh and ll and ph and pl):
            return
        hh, hl = lh.price > ph.price, ll.price > pl.price
        lo_h, lo_l = lh.price < ph.price, ll.price < pl.price
        if hh and hl:
            self.trend = "up"
        elif lo_h and lo_l:
            self.trend = "down"
        else:
            self.trend = "range"

    def on_closed_bar(self, bar: Bar) -> List[StructureEvent]:
        """Detect breaks. Uses bar CLOSE, not wick -- a wick through a level is a
        liquidity sweep, which is the opposite signal to a genuine break. This
        distinction is the difference between getting trapped and trapping."""
        out: List[StructureEvent] = []
        lh, ll = self._last_high, self._last_low

        if lh and bar.c > lh.price:
            ev = StructureEvent.BOS_UP if self.trend == "up" else StructureEvent.CHOCH_UP
            out.append(ev)
            if ev is StructureEvent.CHOCH_UP:
                self.trend = "up"
        if ll and bar.c < ll.price:
            ev = StructureEvent.BOS_DOWN if self.trend == "down" else StructureEvent.CHOCH_DOWN
            out.append(ev)
            if ev is StructureEvent.CHOCH_DOWN:
                self.trend = "down"

        for ev in out:
            self.events.append((bar.t_close_ns, ev))
        if len(self.events) > self._max_events:
            del self.events[:-self._max_events]
        return out

    def swept_liquidity(self, bar: Bar) -> Optional[str]:
        """Wick beyond a swing that CLOSES back inside = stop hunt / sweep.

        This is the single highest-value micro-pattern in the whole engine: it
        marks the moment a cohort of traders was trapped, which is precisely the
        fuel a reversal needs."""
        lh, ll = self._last_high, self._last_low
        if lh and bar.h > lh.price and bar.c < lh.price:
            return "sweep_high"
        if ll and bar.l < ll.price and bar.c > ll.price:
            return "sweep_low"
        return None
