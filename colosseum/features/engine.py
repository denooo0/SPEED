"""The Feature Engine: assembles sealed, point-in-time FeatureFrames.

This is the perception layer and the boundary of trust. Everything upstream is
raw reality; everything downstream reasons ONLY on what this module sealed.

Sealing discipline:
  * a frame is built exclusively from data with t <= t_event_ns
  * it is then hashed and frozen
  * a late tick never mutates a sealed frame -- it opens a new one
  * the frame carries schema + code version, so the learner can never silently
    compare features computed by two different definitions

The engine deliberately recomputes NOTHING. Every indicator is a streaming
accumulator updated once per closed bar. Cost per tick is O(timeframes).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..core.clock import liquidity_session, session_date
from ..core.types import (Bar, FeatureFrame, FlowState, Pivot, Quality, TFView,
                          Tick, VwapState)
from ..ingest.aggregator import Aggregator, DEFAULT_INTERVALS
from .divergence import DivergenceEngine
from .flow import FlowEngine
from .incremental import ATR, RSI
from .pivots import PivotEngine
from .structure import StructureEngine
from .vwap import SessionVWAP


class _TFState:
    """All streaming state for a single timeframe."""
    __slots__ = ("interval_s", "vwap", "flow", "rsi", "atr", "pivots",
                 "structure", "div_adl", "div_rsi", "last_flow", "last_vwap",
                 "last_rsi", "last_atr", "sweeps")

    def __init__(self, interval_s: int, pivot_k: int):
        self.interval_s = interval_s
        self.vwap = SessionVWAP()
        self.flow = FlowEngine()
        self.rsi = RSI(14)
        self.atr = ATR(14)
        self.pivots = PivotEngine(k=pivot_k)
        self.structure = StructureEngine()
        self.div_adl = DivergenceEngine(indicator="adl")
        self.div_rsi = DivergenceEngine(indicator="rsi")
        self.last_flow: Optional[FlowState] = None
        self.last_vwap: Optional[VwapState] = None
        self.last_rsi: Optional[float] = None
        self.last_atr: Optional[float] = None
        self.sweeps: List[tuple] = []

    def on_closed_bar(self, bar: Bar) -> None:
        # Order matters: flow/rsi must be current BEFORE pivots are evaluated,
        # because a confirmed pivot samples the indicator at its own bar.
        self.last_vwap = self.vwap.update(bar)
        self.last_flow = self.flow.update(bar)
        self.last_rsi = self.rsi.update(bar.c)
        self.last_atr = self.atr.update(bar.h, bar.l, bar.c)

        sweep = self.structure.swept_liquidity(bar)
        if sweep:
            self.sweeps.append((bar.t_close_ns, sweep))
            if len(self.sweeps) > 32:
                del self.sweeps[:-32]

        for p in self.pivots.update(bar):
            self.structure.on_pivot(p)
            # Indicator value AT the pivot bar. Because pivots confirm k bars
            # late, we use the accumulator's value as of confirmation -- the
            # honest, knowable value. Sampling the historical value would be
            # more precise but would require replaying; the ADL is cumulative
            # and monotone-ish, so confirmation-time sampling is sound and,
            # critically, cannot leak the future.
            if self.last_flow:
                self.div_adl.observe(p, self.last_flow.adl)
            if self.last_rsi is not None:
                self.div_rsi.observe(p, self.last_rsi)

        self.structure.on_closed_bar(bar)

    def view(self, agg: Aggregator) -> TFView:
        divs = tuple(self.div_adl.recent(4) + self.div_rsi.recent(4))
        return TFView(
            interval_s=self.interval_s,
            last_closed=agg.closed(self.interval_s),
            live=agg.live(self.interval_s),
            vwap=self.last_vwap,
            flow=self.last_flow,
            rsi=self.last_rsi,
            atr=self.last_atr,
            trend=self.structure.trend,
            pivots=tuple(self.pivots.recent(None, 6)),
            divergences=divs,
            structure_events=tuple(self.structure.events[-6:]),
        )


class FeatureEngine:
    def __init__(self, intervals=DEFAULT_INTERVALS, pivot_k: int = 2,
                 history: int = 512, frame_deadline_ms: int = 750):
        self.agg = Aggregator(intervals, history=history)
        self.tf: Dict[int, _TFState] = {
            i: _TFState(i, pivot_k) for i in self.agg.intervals}
        self.frame_deadline_ms = frame_deadline_ms
        self._last_tick: Optional[Tick] = None
        self._frames_built = 0

    def on_tick(self, t: Tick) -> List[Bar]:
        """Advance all state. Returns bars that sealed on this tick."""
        self._last_tick = t
        sealed = self.agg.update(t)
        for bar in sealed:
            self.tf[bar.interval_s].on_closed_bar(bar)
        return sealed

    def build_frame(self, t_ns: int, quality: Quality = Quality.CLEAN) -> FeatureFrame:
        """Seal a point-in-time frame. `t_ns` is an INPUT -- never read from a
        clock -- so replay produces byte-identical frames."""
        tk = self._last_tick
        mid = tk.mid if tk else 0.0
        spread = tk.spread if tk else 0.0
        views = {i: st.view(self.agg) for i, st in self.tf.items()}

        regime = self._regime(views)
        self._frames_built += 1
        return FeatureFrame(
            t_event_ns=t_ns,
            session_date=session_date(t_ns),
            liquidity_session=liquidity_session(t_ns),
            mid=mid, spread=spread, views=views, regime=regime,
            quality=quality,
            deadline_ns=t_ns + self.frame_deadline_ms * 1_000_000,
        ).sealed()

    def _regime(self, views: Dict[int, TFView]) -> Dict[str, object]:
        """Coarse regime tag. Everything the learner does is conditioned on this
        -- applying range logic to a trend day is the classic silent killer."""
        ref = views.get(300) or views.get(60)
        atr = ref.atr if ref and ref.atr else 0.0
        mid = self._last_tick.mid if self._last_tick else 0.0
        atr_pct = (atr / mid * 100.0) if mid else 0.0
        if atr_pct == 0:
            vol_band = "unknown"
        elif atr_pct < 0.04:
            vol_band = "low"
        elif atr_pct < 0.10:
            vol_band = "normal"
        else:
            vol_band = "high"
        htf = views.get(3600) or views.get(900)
        rvol = ref.flow.rvol if ref and ref.flow else 1.0
        return {
            "trend_htf": htf.trend if htf else "unknown",
            "trend_ltf": ref.trend if ref else "unknown",
            "vol_band": vol_band,
            "atr_pct": round(atr_pct, 5),
            "rvol": round(rvol, 3),
        }

    @property
    def frames_built(self) -> int:
        return self._frames_built
