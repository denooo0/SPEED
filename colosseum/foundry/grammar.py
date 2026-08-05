"""A typed grammar for tradeable hypotheses.

Lenses are not hand-written any more. They are EXPRESSIONS in a formal grammar,
generated, mutated, and recombined by the Foundry.

Why a grammar rather than free-form code generation:

  * every generated lens is syntactically valid by construction — no dead
    candidates, no eval() of model-written strings
  * the search space is bounded and enumerable, so "number of trials" is a real
    number you can deflate against (see learn/deflated.py). Free-form generation
    makes n_trials unknowable, which makes overfitting-correction impossible.
  * expressions are HUMAN READABLE. A promoted lens can be read, argued with,
    and understood. An opaque promoted lens is a liability.
  * mutation and crossover are structural operations on a tree, so offspring are
    always valid.

THE DIVERSITY INSIGHT ENCODED HERE. Every lens declares three things beyond its
predicate: a HORIZON, an OBJECTIVE, and a REGIME filter. Those, not the choice
of indicator, are what actually decorrelate errors. A 5-minute reversal
specialist and a 4-hour continuation specialist disagree structurally even when
reading identical features -- because they are answering different questions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


class Objective(str, Enum):
    """WHAT KIND OF QUESTION the lens asks. A primary diversity axis."""
    REVERSAL = "reversal"           # fade the move
    CONTINUATION = "continuation"   # ride the move
    BREAKOUT = "breakout"           # join the expansion
    FAILURE = "failure"             # fade the failed expansion (trap)
    LIQUIDITY = "liquidity"         # provide liquidity at extremes
    COMPRESSION = "compression"     # position before expansion


class Horizon(str, Enum):
    """HOW LONG the claim is about. The second diversity axis."""
    SCALP = "scalp"       # ~5-15 bars
    INTRADAY = "intraday"  # ~30-90 bars
    SWING = "swing"       # ~200+ bars

    @property
    def bars(self) -> int:
        return {"scalp": 12, "intraday": 60, "swing": 240}[self.value]


class RegimeFilter(str, Enum):
    """WHEN the lens is allowed to speak. The third diversity axis, and the one
    that most reduces error correlation: two lenses that never fire at the same
    time cannot have correlated errors."""
    ANY = "any"
    TRENDING = "trending"
    BALANCED = "balanced"
    HIGH_VOL = "high_vol"
    LOW_VOL = "low_vol"
    LONDON = "london"
    NY = "ny"
    ASIA = "asia"


class Op(str, Enum):
    GT = ">"
    LT = "<"
    CROSS_UP = "crosses_above"
    CROSS_DN = "crosses_below"
    RISING = "rising"
    FALLING = "falling"


# Feature vocabulary. Every name must exist in the FeatureFrame digest the
# Foundry evaluates against. Keeping this explicit (rather than "any key") is
# what makes the search space countable.
FEATURES: Dict[str, Tuple[float, float]] = {
    # name: (typical low, typical high) used to seed sensible thresholds
    "dist_sigma": (-3.0, 3.0),
    "rvol": (0.3, 3.0),
    "effort_result": (0.3, 3.0),
    "delta_ratio": (-0.8, 0.8),
    "adl_slope": (-500.0, 500.0),
    "cmf": (-0.5, 0.5),
    "rsi": (15.0, 85.0),
    "atr_pct": (0.01, 0.25),
    "body_ratio": (0.0, 1.0),
    "upper_wick_ratio": (0.0, 0.8),
    "lower_wick_ratio": (0.0, 0.8),
    "vpin": (0.05, 0.6),
    "kyle_lambda": (0.0, 0.01),
    "amihud": (0.0, 5.0),
    "dist_to_vpoc": (-5.0, 5.0),
    "in_value_area": (0.0, 1.0),
    "momentum_regime": (0.0, 1.0),
    "divergence_strength": (0.0, 1.0),
    "swept_high": (0.0, 1.0),
    "swept_low": (0.0, 1.0),
    "bars_since_pivot": (0.0, 50.0),
    "session_pos": (0.0, 1.0),        # position through the session
    "range_pct_of_atr": (0.0, 3.0),
}


# ---------------------------------------------------------------- predicates

@dataclass(frozen=True)
class Predicate:
    """One atomic condition. Deliberately simple: complexity lives in the
    CONJUNCTION of a few simple terms, not in any single baroque term. Deep
    expression trees are an overfitting machine at these sample sizes."""
    feature: str
    op: Op
    value: float = 0.0
    other: Optional[str] = None     # for feature-vs-feature comparisons

    def evaluate(self, f: Dict[str, float],
                 prev: Optional[Dict[str, float]] = None) -> bool:
        v = f.get(self.feature)
        if v is None:
            return False
        if self.op is Op.GT:
            rhs = f.get(self.other, self.value) if self.other else self.value
            return v > rhs
        if self.op is Op.LT:
            rhs = f.get(self.other, self.value) if self.other else self.value
            return v < rhs
        if prev is None:
            return False
        pv = prev.get(self.feature)
        if pv is None:
            return False
        if self.op is Op.CROSS_UP:
            return pv <= self.value < v
        if self.op is Op.CROSS_DN:
            return pv >= self.value > v
        if self.op is Op.RISING:
            return v > pv
        if self.op is Op.FALLING:
            return v < pv
        return False

    def describe(self) -> str:
        if self.op in (Op.RISING, Op.FALLING):
            return f"{self.feature} {self.op.value}"
        if self.other:
            return f"{self.feature} {self.op.value} {self.other}"
        return f"{self.feature} {self.op.value} {self.value:.4g}"


@dataclass(frozen=True)
class LensSpec:
    """A complete, executable trading hypothesis.

    Conditions are ANDed. That is a deliberate capacity limit: a conjunction of
    2-4 simple terms is roughly the complexity that survives out-of-sample at
    the sample sizes a live engine accumulates. Richer logic is available via
    the objective/regime axes rather than via deeper boolean trees.
    """
    lens_id: str
    conditions: Tuple[Predicate, ...]
    objective: Objective
    horizon: Horizon
    regime: RegimeFilter
    direction_long: bool             # direction when conditions fire
    generation: int = 0
    parents: Tuple[str, ...] = ()

    def fires(self, f: Dict[str, float],
              prev: Optional[Dict[str, float]] = None) -> bool:
        if not self._regime_ok(f):
            return False
        return all(c.evaluate(f, prev) for c in self.conditions)

    def _regime_ok(self, f: Dict[str, float]) -> bool:
        r = self.regime
        if r is RegimeFilter.ANY:
            return True
        if r is RegimeFilter.TRENDING:
            return f.get("momentum_regime", 0.0) > 0.5
        if r is RegimeFilter.BALANCED:
            return f.get("momentum_regime", 0.0) <= 0.5
        if r is RegimeFilter.HIGH_VOL:
            return f.get("atr_pct", 0.0) > 0.08
        if r is RegimeFilter.LOW_VOL:
            return f.get("atr_pct", 0.0) <= 0.08
        if r is RegimeFilter.LONDON:
            return f.get("sess_london", 0.0) > 0.5
        if r is RegimeFilter.NY:
            return f.get("sess_ny", 0.0) > 0.5
        if r is RegimeFilter.ASIA:
            return f.get("sess_asia", 0.0) > 0.5
        return True

    @property
    def complexity(self) -> int:
        return len(self.conditions) + (0 if self.regime is RegimeFilter.ANY else 1)

    def describe(self) -> str:
        conds = " AND ".join(c.describe() for c in self.conditions)
        reg = "" if self.regime is RegimeFilter.ANY else f" [{self.regime.value} only]"
        return (f"{'LONG' if self.direction_long else 'SHORT'} "
                f"({self.objective.value}/{self.horizon.value}){reg} when {conds}")

    def to_dict(self) -> Dict[str, Any]:
        return {"lens_id": self.lens_id, "objective": self.objective.value,
                "horizon": self.horizon.value, "regime": self.regime.value,
                "direction": "long" if self.direction_long else "short",
                "conditions": [c.describe() for c in self.conditions],
                "generation": self.generation, "parents": list(self.parents),
                "description": self.describe()}

    def signature(self) -> str:
        """Structural identity — used to reject exact duplicates cheaply before
        the expensive behavioral test runs."""
        parts = sorted(c.describe() for c in self.conditions)
        return (f"{self.objective.value}|{self.horizon.value}|"
                f"{self.regime.value}|{'L' if self.direction_long else 'S'}|"
                + "&".join(parts))


# ---------------------------------------------------------------- semantics

# Which objectives make sense with which feature families. This is DOMAIN
# KNOWLEDGE injected as a prior, and it matters enormously: unconstrained random
# search wastes ~all its budget on incoherent hypotheses ("go long because
# volume is low and RSI is high and price is at the top of the value area").
# Seeding the search with coherent structure is the difference between a search
# that finds something and one that burns compute.
OBJECTIVE_FEATURES: Dict[Objective, List[str]] = {
    Objective.REVERSAL: ["dist_sigma", "rsi", "effort_result", "divergence_strength",
                         "swept_high", "swept_low", "upper_wick_ratio",
                         "lower_wick_ratio", "vpin", "dist_to_vpoc"],
    Objective.CONTINUATION: ["adl_slope", "cmf", "delta_ratio", "momentum_regime",
                             "body_ratio", "rvol", "range_pct_of_atr"],
    Objective.BREAKOUT: ["rvol", "range_pct_of_atr", "atr_pct", "delta_ratio",
                         "body_ratio", "in_value_area", "kyle_lambda"],
    Objective.FAILURE: ["swept_high", "swept_low", "upper_wick_ratio",
                        "lower_wick_ratio", "effort_result", "delta_ratio",
                        "in_value_area", "rvol"],
    Objective.LIQUIDITY: ["dist_sigma", "dist_to_vpoc", "amihud", "rvol",
                          "vpin", "in_value_area"],
    Objective.COMPRESSION: ["atr_pct", "range_pct_of_atr", "rvol", "bars_since_pivot",
                            "body_ratio", "amihud"],
}
