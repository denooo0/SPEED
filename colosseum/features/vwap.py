"""Session-anchored VWAP with true volume-weighted standard deviation bands.

Most implementations get the bands wrong: they take a simple standard deviation
of price and call it a VWAP band. The correct band is the volume-weighted
deviation of typical price about the VWAP itself:

    VWAP = Sum(tp_i * v_i) / Sum(v_i)
    Var  = Sum(v_i * (tp_i - VWAP)^2) / Sum(v_i)

computed with Welford so it stays stable across a 23-hour session. This matters
because the entire mean-reversion lens keys off distance-in-sigma; a wrong sigma
means every fade signal is mispriced.
"""
from __future__ import annotations

from typing import Optional

from ..core.clock import session_start_ns
from ..core.types import Bar, VwapState
from .incremental import Welford


class SessionVWAP:
    """Resets at the DST-correct session roll. Anchor is an input, never derived
    from wall-clock."""

    __slots__ = ("_w", "_pv", "_v", "_anchor")

    def __init__(self) -> None:
        self._w = Welford()
        self._pv = 0.0
        self._v = 0.0
        self._anchor = 0

    def update(self, bar: Bar) -> Optional[VwapState]:
        anchor = session_start_ns(bar.t_open_ns)
        if anchor != self._anchor:
            self._w.reset()
            self._pv = self._v = 0.0
            self._anchor = anchor

        tp, v = bar.typical, bar.volume
        if v <= 0:
            v = 0.0
        self._pv += tp * v
        self._v += v
        self._w.update(tp, v)

        if self._v <= 0:
            return None
        vwap = self._pv / self._v
        sigma = self._w.stdev
        dist = (bar.c - vwap) / sigma if sigma > 1e-12 else 0.0
        return VwapState(
            vwap=vwap, sigma=sigma,
            upper1=vwap + sigma, lower1=vwap - sigma,
            upper2=vwap + 2 * sigma, lower2=vwap - 2 * sigma,
            dist_sigma=dist, cum_volume=self._v, anchor_ns=anchor)


class AnchoredVWAP:
    """VWAP anchored to an arbitrary event (session high/low, a news spike, a
    structural break). The mean-reverter lens uses these to test whether a level
    is being defended."""

    __slots__ = ("_w", "_pv", "_v", "anchor_ns")

    def __init__(self, anchor_ns: int):
        self._w = Welford()
        self._pv = 0.0
        self._v = 0.0
        self.anchor_ns = anchor_ns

    def update(self, bar: Bar) -> Optional[VwapState]:
        if bar.t_open_ns < self.anchor_ns:
            return None
        tp, v = bar.typical, max(bar.volume, 0.0)
        self._pv += tp * v
        self._v += v
        self._w.update(tp, v)
        if self._v <= 0:
            return None
        vwap = self._pv / self._v
        sigma = self._w.stdev
        return VwapState(
            vwap=vwap, sigma=sigma,
            upper1=vwap + sigma, lower1=vwap - sigma,
            upper2=vwap + 2 * sigma, lower2=vwap - 2 * sigma,
            dist_sigma=(bar.c - vwap) / sigma if sigma > 1e-12 else 0.0,
            cum_volume=self._v, anchor_ns=self.anchor_ns)
