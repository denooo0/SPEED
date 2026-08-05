"""Causal swing-pivot detection -- THE REPAINT FIREWALL.

A fractal pivot at bar i requires k bars on BOTH sides. That means the pivot at
bar i is not knowable until bar i+k has closed. Every charting package draws it
at bar i and every naive backtest therefore trades on information from the
future. This is the most common, most expensive bug in systematic trading.

Here, a Pivot carries two timestamps:
    t_ns            -- when the extreme actually occurred
    t_confirmed_ns  -- when it became knowable

and NOTHING downstream is permitted to key off t_ns for decision-making. The
type enforces the distinction; the divergence engine and structure engine both
consume only confirmed pivots.
"""
from __future__ import annotations

from typing import List, Optional

from ..core.ringbuf import Ring
from ..core.types import Bar, Pivot, PivotKind


class PivotEngine:
    """Fractal pivot detector with k-bar confirmation on each side."""

    __slots__ = ("k", "_bars", "_idx", "pivots", "_max_pivots")

    def __init__(self, k: int = 2, max_pivots: int = 128, history: int = 256):
        if k < 1:
            raise ValueError("k must be >= 1")
        self.k = k
        self._bars: Ring[Bar] = Ring(max(history, 2 * k + 3))
        self._idx = 0
        self.pivots: Ring[Pivot] = Ring(max_pivots)
        self._max_pivots = max_pivots

    def update(self, bar: Bar) -> List[Pivot]:
        """Feed a CLOSED bar. Returns pivots newly CONFIRMED by this bar."""
        self._bars.push(bar)
        self._idx += 1
        out: List[Pivot] = []

        n = len(self._bars)
        need = 2 * self.k + 1
        if n < need:
            return out

        # Candidate sits k bars back from the newest -- now fully surrounded.
        c = n - 1 - self.k
        cand = self._bars[c]
        left = [self._bars[j] for j in range(c - self.k, c)]
        right = [self._bars[j] for j in range(c + 1, c + 1 + self.k)]

        # Strict on the left, non-strict on the right: breaks ties deterministically
        # and prevents a flat double-top from registering two pivots.
        if all(cand.h > b.h for b in left) and all(cand.h >= b.h for b in right):
            p = Pivot(t_ns=cand.t_open_ns, t_confirmed_ns=bar.t_close_ns,
                      price=cand.h, kind=PivotKind.HIGH,
                      interval_s=cand.interval_s, bar_index=self._idx - 1 - self.k)
            self.pivots.push(p)
            out.append(p)

        if all(cand.l < b.l for b in left) and all(cand.l <= b.l for b in right):
            p = Pivot(t_ns=cand.t_open_ns, t_confirmed_ns=bar.t_close_ns,
                      price=cand.l, kind=PivotKind.LOW,
                      interval_s=cand.interval_s, bar_index=self._idx - 1 - self.k)
            self.pivots.push(p)
            out.append(p)

        return out

    def recent(self, kind: Optional[PivotKind] = None, n: int = 2) -> List[Pivot]:
        """Most recent n confirmed pivots, oldest-first."""
        items = [p for p in self.pivots if kind is None or p.kind is kind]
        return items[-n:] if n <= len(items) else items

    def last_high(self) -> Optional[Pivot]:
        r = self.recent(PivotKind.HIGH, 1)
        return r[0] if r else None

    def last_low(self) -> Optional[Pivot]:
        r = self.recent(PivotKind.LOW, 1)
        return r[0] if r else None
