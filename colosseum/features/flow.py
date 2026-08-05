"""Accumulation / distribution footprints -- the 'who is really doing what' layer.

This is the heart of the Wyckoff lens. Price is the receipt; these are the
transactions. The single most valuable derived quantity here is EFFORT vs
RESULT: high volume that fails to move price is absorption, and absorption at
an extreme is the fingerprint of distribution (or accumulation) by someone large
enough to need to hide.
"""
from __future__ import annotations

from typing import Optional

from ..core.ringbuf import Ring
from ..core.types import Bar, FlowState
from .incremental import RollingMean, Slope


class FlowEngine:
    """ADL + OBV + CMF + RVOL + delta, streaming, on CLOSED bars only."""

    __slots__ = ("_adl", "_obv", "_slope", "_rvol", "_mfv", "_vol_win",
                 "_range_mean", "_prev_close", "_cmf_period")

    def __init__(self, rvol_window: int = 50, adl_slope_window: int = 20,
                 cmf_period: int = 21):
        self._adl = 0.0
        self._obv = 0.0
        self._slope = Slope(adl_slope_window)
        self._rvol = RollingMean(rvol_window)
        self._range_mean = RollingMean(rvol_window)
        self._mfv: Ring[float] = Ring(cmf_period)
        self._vol_win: Ring[float] = Ring(cmf_period)
        self._prev_close: Optional[float] = None
        self._cmf_period = cmf_period

    @staticmethod
    def money_flow_multiplier(b: Bar) -> float:
        """MFM = ((C-L) - (H-C)) / (H-L), in [-1, +1].

        +1 = closed on the high (buyers owned the bar)
        -1 = closed on the low  (sellers owned the bar)
        A doji at the midpoint contributes nothing -- correct: it was a fair fight.
        """
        rng = b.h - b.l
        if rng <= 0:
            return 0.0
        return ((b.c - b.l) - (b.h - b.c)) / rng

    def update(self, b: Bar) -> FlowState:
        mfm = self.money_flow_multiplier(b)
        mfv = mfm * b.volume

        # -- Accumulation/Distribution Line (cumulative money flow volume)
        self._adl += mfv
        adl_slope = self._slope.update(self._adl)

        # -- On-Balance Volume (direction-weighted cumulative volume)
        if self._prev_close is not None:
            if b.c > self._prev_close:
                self._obv += b.volume
            elif b.c < self._prev_close:
                self._obv -= b.volume
        self._prev_close = b.c

        # -- Chaikin Money Flow: MFV over volume, across the period
        self._mfv.push(mfv)
        self._vol_win.push(b.volume)
        vsum = sum(self._vol_win)
        cmf = (sum(self._mfv) / vsum) if vsum > 0 else 0.0

        # -- Relative volume
        mean_v = self._rvol.value
        rvol = (b.volume / mean_v) if mean_v > 0 else 1.0
        self._rvol.update(b.volume)

        # -- Effort vs result: volume expended per unit of range achieved.
        #    >> 1 means heavy effort, little movement => ABSORPTION.
        mean_r = self._range_mean.value
        self._range_mean.update(b.range)
        norm_range = (b.range / mean_r) if mean_r > 0 else 1.0
        effort_result = rvol / norm_range if norm_range > 1e-9 else rvol

        delta_ratio = (b.delta / b.volume) if b.volume > 0 else 0.0

        return FlowState(
            adl=self._adl, adl_slope=adl_slope, obv=self._obv, cmf=cmf,
            rvol=rvol, delta_ratio=delta_ratio, effort_result=effort_result)

    @property
    def adl(self) -> float:
        return self._adl

    @property
    def obv(self) -> float:
        return self._obv
