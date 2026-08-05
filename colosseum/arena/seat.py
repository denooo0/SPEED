"""Seats: the two-stage strategist.

Stage 1 -- PRE-FILTER (code, cheap, runs every second on every frame)
Stage 2 -- STRATEGIST (LLM, expensive, runs only on flagged candidates)

This split is what makes "scan every second forever" affordable. The code
watches continuously; the mind is summoned only when there is something worth
thinking about. It also means the expensive component's cost scales with
OPPORTUNITY, not with time.

The pre-filters below are real, runnable implementations of the three lenses.
They are deliberately conservative: a pre-filter that flags everything defeats
the purpose and saturates the LLM queue (see the backpressure detector).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..core.types import (DivKind, FeatureFrame, Proposal, Side, StandDown,
                          TFView)


@dataclass(frozen=True)
class Candidate:
    """Pre-filter output: enough context for the strategist to reason about,
    plus the numeric evidence that triggered it."""
    seat_id: str
    score: float
    bias: Side
    trigger: str
    evidence: Dict[str, float]
    anchor_level: float          # the STRUCTURAL level the bandit tunes around
    anchor_kind: str             # what that level is (swing_high, vwap, ...)


class PreFilter(ABC):
    seat_id: str = "?"
    lens: str = "?"
    threshold: float = 0.5

    @abstractmethod
    def evaluate(self, f: FeatureFrame) -> Tuple[float, Optional[Candidate], str]:
        """-> (score, candidate_or_None, reason_if_standing_down)"""

    @staticmethod
    def _v(f: FeatureFrame, interval: int) -> Optional[TFView]:
        return f.views.get(interval)


class WyckoffPreFilter(PreFilter):
    """Lens A -- effort vs result. Hunts absorption: heavy volume that fails to
    move price, especially when the A/D line contradicts the candle."""
    seat_id, lens = "A", "Wyckoff Accountant"

    def evaluate(self, f: FeatureFrame):
        v = self._v(f, 300) or self._v(f, 60)
        if not v or not v.flow or not v.last_closed:
            return 0.0, None, "insufficient state"
        fl, bar = v.flow, v.last_closed

        if fl.rvol < 1.3:
            return 0.0, None, f"no effort (rvol {fl.rvol:.2f} < 1.3)"
        if fl.effort_result < 1.4:
            return 0.0, None, (f"effort matched by result "
                               f"(e/r {fl.effort_result:.2f}) -- not absorption")

        # Absorption: heavy effort, poor result. Direction from who lost.
        adl_contradicts = (bar.bullish and fl.adl_slope < 0) or \
                          ((not bar.bullish) and fl.adl_slope > 0)
        score = min(1.0, (fl.effort_result - 1.4) / 2.0 +
                    (0.25 if adl_contradicts else 0.0) +
                    min(abs(fl.delta_ratio), 0.5) * 0.3)
        if score < self.threshold:
            return score, None, f"absorption too weak (score {score:.2f})"

        bias = Side.SHORT if bar.bullish else Side.LONG   # fade the exhausted side
        anchor = bar.h if bias is Side.SHORT else bar.l
        return score, Candidate(
            self.seat_id, score, bias, "absorption",
            {"rvol": fl.rvol, "effort_result": fl.effort_result,
             "adl_slope": fl.adl_slope, "delta_ratio": fl.delta_ratio,
             "body_ratio": bar.body_ratio},
            anchor, "swing_extreme"), ""


class DivergencePreFilter(PreFilter):
    """Lens B -- the seam between price and its footprints. Fires only on
    CONFIRMED divergences, and only recent ones."""
    seat_id, lens = "B", "Divergence Hunter"

    def evaluate(self, f: FeatureFrame):
        best, best_v = None, None
        for interval in (300, 900, 60):
            v = self._v(f, interval)
            if not v or not v.divergences:
                continue
            for d in v.divergences:
                if d.kind in (DivKind.HARMONY_UP, DivKind.HARMONY_DOWN):
                    continue
                age_s = (f.t_event_ns - d.t_confirmed_ns) / 1e9
                if age_s > interval * 6:
                    continue                     # stale divergence is not a signal
                if best is None or d.strength > best.strength:
                    best, best_v = d, v
        if best is None:
            return 0.0, None, "no fresh confirmed divergence"

        # Multi-timeframe stacking: the same-direction divergence on another
        # timeframe is the strongest confirmation this lens can get.
        stack = sum(1 for iv in (60, 300, 900)
                    if (vv := self._v(f, iv)) and
                    any(dd.kind == best.kind for dd in vv.divergences))
        score = min(1.0, best.strength * 2.0 + 0.15 * (stack - 1))
        if score < self.threshold:
            return score, None, f"divergence too weak (score {score:.2f})"

        bearish = best.kind in (DivKind.REG_BEAR, DivKind.HID_BEAR)
        bias = Side.SHORT if bearish else Side.LONG
        return score, Candidate(
            self.seat_id, score, bias, best.kind.value,
            {"strength": best.strength, "stack": float(stack),
             "i1": best.i1, "i2": best.i2},
            best.p2[1], "divergence_pivot"), ""


class VwapPreFilter(PreFilter):
    """Lens C -- fair value. Fades exhausted stretches beyond 2 sigma; rides
    expansion through the band."""
    seat_id, lens = "C", "VWAP Mean-Reverter"

    def evaluate(self, f: FeatureFrame):
        v = self._v(f, 300) or self._v(f, 60)
        if not v or not v.vwap or not v.flow or not v.last_closed:
            return 0.0, None, "insufficient state"
        vw, fl, bar = v.vwap, v.flow, v.last_closed
        d = vw.dist_sigma

        if abs(d) < 1.8:
            # Explicit, honest stand-down: chop is not a signal.
            return 0.0, None, (f"inside VWAP inner bands ({d:+.2f} sigma) "
                               "-- no edge, standing down")

        exhausting = fl.rvol < 1.2 or fl.effort_result > 1.5
        if not exhausting:
            return 0.2, None, (f"stretched {d:+.2f} sigma but volume still "
                               "expanding -- this is a band walk, not a fade")

        score = min(1.0, (abs(d) - 1.8) / 1.2 + (0.25 if fl.effort_result > 1.5 else 0.0))
        if score < self.threshold:
            return score, None, f"stretch insufficient (score {score:.2f})"

        bias = Side.SHORT if d > 0 else Side.LONG
        return score, Candidate(
            self.seat_id, score, bias, "two_sigma_exhaustion",
            {"dist_sigma": d, "rvol": fl.rvol, "sigma": vw.sigma,
             "vwap": vw.vwap, "effort_result": fl.effort_result},
            vw.upper2 if d > 0 else vw.lower2, "vwap_band"), ""


class Strategist(ABC):
    """Stage 2. In production this is the LLM running the Part I master prompt.
    The interface is deliberately narrow so the LLM can be swapped, mocked, or
    replayed without touching anything else."""

    @abstractmethod
    def reason(self, f: FeatureFrame, c: Candidate,
               params: Dict[str, float]) -> Optional[Proposal]:
        ...


ALL_PREFILTERS = (WyckoffPreFilter, DivergencePreFilter, VwapPreFilter)
