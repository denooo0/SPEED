"""Harmony & divergence -- the seam between price and its own footprints.

Four divergence classes plus harmony, detected on CONFIRMED pivots only:

    REGULAR BEARISH : price higher-high, indicator lower-high   -> reversal risk
    REGULAR BULLISH : price lower-low,   indicator higher-low   -> reversal risk
    HIDDEN  BEARISH : price lower-high,  indicator higher-high  -> continuation
    HIDDEN  BULLISH : price higher-low,  indicator lower-low    -> continuation
    HARMONY         : both agree                                -> trend confirmed

`strength` normalizes the disagreement so a 2-cent wiggle against a huge ADL
move isn't scored the same as a genuine structural conflict. Without
normalization the learner drowns in trivial divergences -- the classic reason
divergence strategies "stop working."
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..core.ringbuf import Ring
from ..core.types import DivKind, Divergence, Pivot, PivotKind


class DivergenceEngine:
    """Compares confirmed price pivots against an indicator series sampled at
    those same pivots. Indicator values are recorded at pivot time, so no
    look-ahead can leak in through the indicator either."""

    __slots__ = ("indicator", "_highs", "_lows", "min_bars", "max_bars",
                 "min_strength", "found")

    def __init__(self, indicator: str = "adl", min_bars: int = 3,
                 max_bars: int = 100, min_strength: float = 0.08,
                 keep: int = 64):
        self.indicator = indicator
        self._highs: Ring[Tuple[Pivot, float]] = Ring(keep)
        self._lows: Ring[Tuple[Pivot, float]] = Ring(keep)
        self.min_bars = min_bars      # too close together = noise
        self.max_bars = max_bars      # too far apart = unrelated
        self.min_strength = min_strength
        self.found: Ring[Divergence] = Ring(keep)

    def observe(self, pivot: Pivot, indicator_value: float) -> List[Divergence]:
        """Register a newly-confirmed pivot with the indicator's value AT that
        pivot. Returns any divergence/harmony this completes."""
        out: List[Divergence] = []
        if pivot.kind is PivotKind.HIGH:
            self._highs.push((pivot, indicator_value))
            out += self._scan_highs(pivot)
        else:
            self._lows.push((pivot, indicator_value))
            out += self._scan_lows(pivot)
        for d in out:
            self.found.push(d)
        return out

    # ---- internals -------------------------------------------------------

    def _valid_span(self, a: Pivot, b: Pivot) -> bool:
        span = b.bar_index - a.bar_index
        return self.min_bars <= span <= self.max_bars

    @staticmethod
    def _strength(dp_rel: float, di_rel: float) -> float:
        """Geometric-mean style score: BOTH legs must be meaningful. A huge
        indicator move against a nil price move is not a divergence, it's noise."""
        return (abs(dp_rel) * abs(di_rel)) ** 0.5

    def _scan_highs(self, cur: Pivot) -> List[Divergence]:
        if len(self._highs) < 2:
            return []
        (prev, i_prev) = self._highs[-2]
        (_, i_cur) = self._highs[-1]
        if not self._valid_span(prev, cur):
            return []

        dp = (cur.price - prev.price) / prev.price if prev.price else 0.0
        scale = max(abs(i_prev), abs(i_cur), 1e-9)
        di = (i_cur - i_prev) / scale
        s = self._strength(dp * 100.0, di)
        if s < self.min_strength:
            return []

        kind: Optional[DivKind] = None
        if dp > 0 and di < 0:
            kind = DivKind.REG_BEAR      # price up, flow down -> distribution
        elif dp < 0 and di > 0:
            kind = DivKind.HID_BEAR      # lower high, flow stronger -> continuation down
        elif dp > 0 and di > 0:
            kind = DivKind.HARMONY_UP
        if kind is None:
            return []
        return [Divergence(kind=kind, t_confirmed_ns=cur.t_confirmed_ns,
                           indicator=self.indicator,
                           p1=(prev.t_ns, prev.price), p2=(cur.t_ns, cur.price),
                           i1=i_prev, i2=i_cur, strength=s,
                           interval_s=cur.interval_s)]

    def _scan_lows(self, cur: Pivot) -> List[Divergence]:
        if len(self._lows) < 2:
            return []
        (prev, i_prev) = self._lows[-2]
        (_, i_cur) = self._lows[-1]
        if not self._valid_span(prev, cur):
            return []

        dp = (cur.price - prev.price) / prev.price if prev.price else 0.0
        scale = max(abs(i_prev), abs(i_cur), 1e-9)
        di = (i_cur - i_prev) / scale
        s = self._strength(dp * 100.0, di)
        if s < self.min_strength:
            return []

        kind: Optional[DivKind] = None
        if dp < 0 and di > 0:
            kind = DivKind.REG_BULL      # price down, flow up -> accumulation
        elif dp > 0 and di < 0:
            kind = DivKind.HID_BULL      # higher low, flow weaker -> continuation up
        elif dp < 0 and di < 0:
            kind = DivKind.HARMONY_DOWN
        if kind is None:
            return []
        return [Divergence(kind=kind, t_confirmed_ns=cur.t_confirmed_ns,
                           indicator=self.indicator,
                           p1=(prev.t_ns, prev.price), p2=(cur.t_ns, cur.price),
                           i1=i_prev, i2=i_cur, strength=s,
                           interval_s=cur.interval_s)]

    def recent(self, n: int = 5) -> List[Divergence]:
        return self.found.last(n)
