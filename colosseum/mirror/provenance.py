"""MIRROR — level provenance: WHY did they choose that stop, that target?

This is the actual reverse-engineering. Given a signal and the market state at
the moment it was posted, work out which structural feature explains each level.

The method is simple and surprisingly powerful. For every signal, compute a
large set of CANDIDATE ANCHORS from the tape at post time — the prior swing high,
the session low, VWAP, the 2-sigma band, yesterday's close, the round number,
VPOC, an ATR multiple, a Fibonacci retracement, and so on. Then ask: which
anchor, and which offset from it, best explains the level they actually posted?

Aggregate that across hundreds of signals and a rulebook falls out:

    "The stop is placed 0.3 ATR beyond the prior 15m swing, 71% of the time."
    "TP1 is 1.5R, 84% of the time. TP2 is the session high, 62% of the time."
    "Entries cluster at round numbers ending in .00 and .50."

That rulebook IS the strategy, recovered without anyone explaining it. And once
you have it you can do the thing that matters: run it forward yourself, measure
it honestly, and decide whether it deserves a seat.

A subtlety worth stating: matching a level to an anchor is not proof of
causation. Round numbers coincide with swing highs often enough that both will
"explain" the same stop. So the report shows the FULL distribution of candidate
explanations with their match rates, and flags when two anchors are
indistinguishable — rather than declaring a single winner and pretending to
certainty.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple


@dataclass
class Anchor:
    """A structural level that might explain a posted price."""
    name: str
    price: float
    family: str          # structure | vwap | volatility | round | session | profile

    def offset_from(self, px: float) -> float:
        return px - self.price


@dataclass
class MarketContext:
    """Everything knowable at post time. Built from the tick archive."""
    t_ns: int
    mid: float
    atr: float
    anchors: List[Anchor] = field(default_factory=list)

    def add(self, name: str, price: Optional[float], family: str) -> None:
        if price is not None and price > 0:
            self.anchors.append(Anchor(name, price, family))


def build_context(t_ns: int, mid: float, atr: float, *,
                  swing_high: Optional[float] = None,
                  swing_low: Optional[float] = None,
                  prev_swing_high: Optional[float] = None,
                  prev_swing_low: Optional[float] = None,
                  vwap: Optional[float] = None,
                  vwap_u1: Optional[float] = None,
                  vwap_l1: Optional[float] = None,
                  vwap_u2: Optional[float] = None,
                  vwap_l2: Optional[float] = None,
                  session_high: Optional[float] = None,
                  session_low: Optional[float] = None,
                  prev_day_high: Optional[float] = None,
                  prev_day_low: Optional[float] = None,
                  prev_day_close: Optional[float] = None,
                  vpoc: Optional[float] = None,
                  value_area_high: Optional[float] = None,
                  value_area_low: Optional[float] = None) -> MarketContext:
    ctx = MarketContext(t_ns, mid, atr)
    ctx.add("swing_high", swing_high, "structure")
    ctx.add("swing_low", swing_low, "structure")
    ctx.add("prev_swing_high", prev_swing_high, "structure")
    ctx.add("prev_swing_low", prev_swing_low, "structure")
    ctx.add("vwap", vwap, "vwap")
    ctx.add("vwap_+1sd", vwap_u1, "vwap")
    ctx.add("vwap_-1sd", vwap_l1, "vwap")
    ctx.add("vwap_+2sd", vwap_u2, "vwap")
    ctx.add("vwap_-2sd", vwap_l2, "vwap")
    ctx.add("session_high", session_high, "session")
    ctx.add("session_low", session_low, "session")
    ctx.add("prev_day_high", prev_day_high, "session")
    ctx.add("prev_day_low", prev_day_low, "session")
    ctx.add("prev_day_close", prev_day_close, "session")
    ctx.add("vpoc", vpoc, "profile")
    ctx.add("value_area_high", value_area_high, "profile")
    ctx.add("value_area_low", value_area_low, "profile")

    # volatility anchors, both directions
    for k in (0.5, 1.0, 1.5, 2.0, 3.0):
        ctx.add(f"mid+{k}atr", mid + k * atr, "volatility")
        ctx.add(f"mid-{k}atr", mid - k * atr, "volatility")

    # round-number anchors: psychological levels are real and heavily used by
    # discretionary desks, which is exactly who tends to run these channels
    for step in (1.0, 5.0, 10.0, 25.0, 50.0):
        ctx.add(f"round_{step:g}", round(mid / step) * step, "round")
    # gold-specific: .00 and .50 handles
    ctx.add("handle_.00", math.floor(mid) + 0.0, "round")
    ctx.add("handle_.50", math.floor(mid) + 0.5, "round")

    # Fibonacci retracements of the current session range
    if session_high and session_low and session_high > session_low:
        rng = session_high - session_low
        for f in (0.236, 0.382, 0.5, 0.618, 0.786):
            ctx.add(f"fib_{f}_up", session_low + rng * f, "structure")
            ctx.add(f"fib_{f}_dn", session_high - rng * f, "structure")
    return ctx


@dataclass
class Explanation:
    anchor: str
    family: str
    offset: float
    offset_atr: float
    abs_error: float

    def to_dict(self) -> Dict[str, object]:
        return {"anchor": self.anchor, "family": self.family,
                "offset": round(self.offset, 3),
                "offset_atr": round(self.offset_atr, 3),
                "abs_error": round(self.abs_error, 3)}


def explain_level(price: float, ctx: MarketContext,
                  tolerance_atr: float = 1.2,
                  top_k: int = 8) -> List[Explanation]:
    """Which anchors sit close enough to plausibly explain this level?

    Returns ALL plausible explanations, ranked — not one winner. When several
    anchors coincide the honest answer is "these are indistinguishable on this
    sample," and forcing a single attribution would manufacture confidence.

    TOLERANCE IS DELIBERATELY WIDE (1.2 ATR). A tight tolerance can only ever
    discover rules with a ZERO offset — "the stop sits exactly on the swing
    high." But real desks place stops BEYOND structure, and a rule like
    "swing high + 0.3 ATR" is invisible to a 0.15 ATR window. The true anchor is
    identified not by proximity but by the CONSISTENCY of its offset across many
    signals: the right anchor is the one whose offset barely varies. Proximity
    finds coincidences; stability finds rules.
    """
    atr = max(ctx.atr, 1e-6)
    tol = tolerance_atr * atr
    hits = []
    for a in ctx.anchors:
        err = abs(price - a.price)
        if err <= tol:
            hits.append(Explanation(a.name, a.family, price - a.price,
                                    (price - a.price) / atr, err))
    hits.sort(key=lambda e: e.abs_error)
    return hits[:top_k]


@dataclass
class RuleCandidate:
    role: str                     # entry | stop | tp1 | tp2 | tp3
    anchor: str
    family: str
    support: int
    total: int
    mean_offset_atr: float
    std_offset_atr: float

    @property
    def match_rate(self) -> float:
        return self.support / self.total if self.total else 0.0

    @property
    def consistency(self) -> float:
        """How tightly clustered is the offset? A rule with a stable offset is a
        RULE; one with a scattered offset is a coincidence."""
        if self.support < 3:
            return 0.0
        return 1.0 / (1.0 + self.std_offset_atr * 4.0)

    def describe(self) -> str:
        sign = "+" if self.mean_offset_atr >= 0 else ""
        return (f"{self.role} = {self.anchor} {sign}{self.mean_offset_atr:.2f} ATR "
                f"({self.match_rate:.0%} of signals, offset sd "
                f"{self.std_offset_atr:.2f} ATR)")

    def to_dict(self) -> Dict[str, object]:
        return {"role": self.role, "anchor": self.anchor, "family": self.family,
                "support": self.support, "total": self.total,
                "match_rate": round(self.match_rate, 4),
                "mean_offset_atr": round(self.mean_offset_atr, 4),
                "std_offset_atr": round(self.std_offset_atr, 4),
                "consistency": round(self.consistency, 4),
                "rule": self.describe()}


class ProvenanceMiner:
    """Accumulates explanations across many signals and extracts the rulebook."""

    def __init__(self, tolerance_atr: float = 1.2):
        self.tolerance_atr = tolerance_atr
        self._obs: Dict[str, Dict[str, List[float]]] = defaultdict(
            lambda: defaultdict(list))
        self._totals: Dict[str, int] = defaultdict(int)
        self._r_multiples: Dict[str, List[float]] = defaultdict(list)

    def observe(self, role: str, price: float, ctx: MarketContext) -> None:
        self._totals[role] += 1
        for e in explain_level(price, ctx, self.tolerance_atr, top_k=6):
            self._obs[role][e.anchor].append(e.offset_atr)

    def observe_r_multiple(self, role: str, r: float) -> None:
        """Targets are often placed at fixed R multiples rather than at
        structure. That is itself a finding, and a very actionable one."""
        self._r_multiples[role].append(r)

    def rules(self, min_support: int = 5, min_rate: float = 0.25
              ) -> List[RuleCandidate]:
        out: List[RuleCandidate] = []
        for role, anchors in self._obs.items():
            total = self._totals[role]
            for anchor, offsets in anchors.items():
                if len(offsets) < min_support:
                    continue
                m = sum(offsets) / len(offsets)
                var = (sum((o - m) ** 2 for o in offsets) / (len(offsets) - 1)
                       if len(offsets) > 1 else 0.0)
                rc = RuleCandidate(role, anchor,
                                   "?", len(offsets), total, m, math.sqrt(var))
                if rc.match_rate >= min_rate:
                    out.append(rc)
        # Rank by CONSISTENCY first. An anchor explaining 90% of levels with a
        # scattered offset is a coincidence of proximity; one explaining 40%
        # with a razor-tight offset is a rule. Weighting consistency more
        # heavily is what separates the two.
        out.sort(key=lambda r: -(r.consistency ** 2 * r.match_rate))
        return out

    def r_multiple_rules(self, min_n: int = 5) -> List[Dict[str, object]]:
        out = []
        for role, vals in self._r_multiples.items():
            if len(vals) < min_n:
                continue
            vals_sorted = sorted(vals)
            med = vals_sorted[len(vals_sorted) // 2]
            m = sum(vals) / len(vals)
            var = sum((v - m) ** 2 for v in vals) / max(len(vals) - 1, 1)
            sd = math.sqrt(var)
            # cluster tightness around the median tells you if it is a FIXED rule
            near = sum(1 for v in vals if abs(v - med) < 0.15)
            out.append({"role": role, "n": len(vals),
                        "median_r": round(med, 3), "mean_r": round(m, 3),
                        "sd_r": round(sd, 3),
                        "share_within_0.15R_of_median": round(near / len(vals), 3),
                        "interpretation": (
                            f"{role} looks like a FIXED {med:.2f}R rule "
                            f"({near/len(vals):.0%} of signals land within "
                            f"0.15R of it)" if near / len(vals) > 0.6 else
                            f"{role} R-multiple varies (sd {sd:.2f}R) — likely "
                            f"placed at structure, not a fixed ratio")})
        return out

    def report(self, min_support: int = 5) -> Dict[str, object]:
        rules = self.rules(min_support)
        by_role: Dict[str, List[Dict[str, object]]] = defaultdict(list)
        for r in rules:
            by_role[r.role].append(r.to_dict())
        ambiguous = []
        for role, rs in by_role.items():
            if len(rs) >= 2 and abs(rs[0]["match_rate"] - rs[1]["match_rate"]) < 0.08:
                ambiguous.append(
                    f"{role}: '{rs[0]['anchor']}' and '{rs[1]['anchor']}' explain "
                    f"the level almost equally well ({rs[0]['match_rate']:.0%} vs "
                    f"{rs[1]['match_rate']:.0%}). These are indistinguishable on "
                    f"this sample — do not pick one and call it the rule.")
        return {
            "rules_by_role": dict(by_role),
            "r_multiple_rules": self.r_multiple_rules(),
            "ambiguous_attributions": ambiguous,
            "note": ("Match rate is not proof of causation. Round numbers and "
                     "swing levels coincide frequently. Treat these as ranked "
                     "hypotheses to test forward, not as recovered source code."),
        }
