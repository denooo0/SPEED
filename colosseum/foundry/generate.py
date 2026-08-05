"""Candidate generation: seeded random, evolutionary, and LLM-proposed.

Three generators feeding one funnel:

  SEEDED RANDOM   coherent random draws from the grammar, constrained by the
                  objective→feature priors so the budget is not burned on
                  incoherent hypotheses
  EVOLUTIONARY    mutation and crossover of proven parents, aimed at the
                  archive's coverage gaps
  LLM-PROPOSED    a model reads the current roster, the coverage gaps, and the
                  lesson library, then proposes hypotheses in structured form

The LLM one is the interesting one, and the division of labour is the point:
**the model proposes, the statistics dispose.** An LLM is genuinely good at
"here is a market situation nobody is covering, what mechanism might exist
there" and genuinely bad at knowing whether it is real. Deflated Sharpe and PBO
do not care how eloquent the hypothesis was.

Everything is deterministic given a seed, so a promotion decision can be
reproduced exactly during an audit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .grammar import (FEATURES, OBJECTIVE_FEATURES, Horizon, LensSpec,
                      Objective, Op, Predicate, RegimeFilter)


class Rng:
    """xorshift32 — deterministic across machines and Python versions."""
    __slots__ = ("s",)

    def __init__(self, seed: int = 20260803):
        self.s = seed & 0xFFFFFFFF or 1

    def u32(self) -> int:
        x = self.s
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.s = x & 0xFFFFFFFF
        return self.s

    def rand(self) -> float:
        return self.u32() / 0xFFFFFFFF

    def pick(self, seq: Sequence):
        return seq[self.u32() % len(seq)]

    def between(self, lo: float, hi: float) -> float:
        return lo + (hi - lo) * self.rand()

    def chance(self, p: float) -> bool:
        return self.rand() < p


# Objectives imply a direction convention. Encoding this as a prior stops the
# generator producing "reversal LONG when price is 3 sigma ABOVE VWAP", which is
# not a reversal trade, it is a bad trade with a reversal label.
DIRECTION_HINTS: Dict[Objective, Dict[str, bool]] = {
    Objective.REVERSAL: {"dist_sigma_high": False, "rsi_high": False,
                         "swept_high": False, "swept_low": True},
    Objective.CONTINUATION: {"adl_slope_high": True, "cmf_high": True,
                             "delta_high": True},
    Objective.BREAKOUT: {"rvol_high": True, "delta_high": True},
    Objective.FAILURE: {"swept_high": False, "swept_low": True},
    Objective.LIQUIDITY: {"dist_sigma_high": False},
    Objective.COMPRESSION: {},
}


class Generator:
    def __init__(self, seed: int = 20260803, max_conditions: int = 3):
        self.rng = Rng(seed)
        self.max_conditions = max_conditions
        self._counter = 0

    def _next_id(self, tag: str) -> str:
        self._counter += 1
        return f"LENS-{tag}-{self._counter:05d}"

    # ---- random ----------------------------------------------------------

    def random_lens(self, objective: Optional[Objective] = None,
                    horizon: Optional[Horizon] = None,
                    regime: Optional[RegimeFilter] = None,
                    generation: int = 0) -> LensSpec:
        obj = objective or self.rng.pick(list(Objective))
        hor = horizon or self.rng.pick(list(Horizon))
        reg = regime if regime is not None else (
            self.rng.pick(list(RegimeFilter)) if self.rng.chance(0.6)
            else RegimeFilter.ANY)

        pool = OBJECTIVE_FEATURES[obj]
        n_cond = 1 + self.rng.u32() % self.max_conditions
        used: List[str] = []
        conds: List[Predicate] = []
        for _ in range(n_cond):
            feat = self.rng.pick(pool)
            if feat in used:
                continue
            used.append(feat)
            conds.append(self._random_predicate(feat))
        if not conds:
            conds = [self._random_predicate(self.rng.pick(pool))]

        direction = self._infer_direction(obj, conds)
        return LensSpec(self._next_id("R"), tuple(conds), obj, hor, reg,
                        direction, generation)

    def _random_predicate(self, feat: str) -> Predicate:
        lo, hi = FEATURES.get(feat, (0.0, 1.0))
        r = self.rng.rand()
        if r < 0.10:
            return Predicate(feat, Op.RISING)
        if r < 0.20:
            return Predicate(feat, Op.FALLING)
        if r < 0.30:
            return Predicate(feat, Op.CROSS_UP, self.rng.between(lo, hi))
        if r < 0.40:
            return Predicate(feat, Op.CROSS_DN, self.rng.between(lo, hi))
        op = Op.GT if self.rng.chance(0.5) else Op.LT
        # Bias thresholds toward the tails: the interesting behaviour lives at
        # extremes, and a threshold near the median just splits the data in half.
        t = self.rng.rand()
        v = lo + (hi - lo) * (t ** 2 if op is Op.LT else 1 - t ** 2)
        return Predicate(feat, op, v)

    def _infer_direction(self, obj: Objective, conds: Sequence[Predicate]) -> bool:
        """Coherent direction from the objective and what the conditions say."""
        score = 0
        for c in conds:
            f, high = c.feature, (c.op in (Op.GT, Op.CROSS_UP, Op.RISING))
            if f == "dist_sigma":
                score += (-1 if high else 1) if obj in (
                    Objective.REVERSAL, Objective.LIQUIDITY) else (1 if high else -1)
            elif f == "rsi":
                score += (-1 if high else 1) if obj is Objective.REVERSAL else 0
            elif f in ("adl_slope", "cmf", "delta_ratio"):
                score += 1 if high else -1
            elif f == "swept_high":
                score += -1 if high else 0
            elif f == "swept_low":
                score += 1 if high else 0
            elif f == "upper_wick_ratio":
                score += -1 if high else 0
            elif f == "lower_wick_ratio":
                score += 1 if high else 0
            elif f == "dist_to_vpoc":
                score += (-1 if high else 1) if obj is Objective.LIQUIDITY else 0
        if score == 0:
            return self.rng.chance(0.5)
        return score > 0

    # ---- evolutionary ----------------------------------------------------

    def mutate(self, parent: LensSpec, rate: float = 0.4) -> LensSpec:
        conds = list(parent.conditions)
        obj, hor, reg = parent.objective, parent.horizon, parent.regime
        direction = parent.direction_long

        r = self.rng.rand()
        if r < 0.35 and conds:
            # perturb a threshold — local search around a working idea
            i = self.rng.u32() % len(conds)
            c = conds[i]
            lo, hi = FEATURES.get(c.feature, (0.0, 1.0))
            span = (hi - lo) * 0.25
            conds[i] = Predicate(c.feature, c.op,
                                 c.value + self.rng.between(-span, span), c.other)
        elif r < 0.55 and len(conds) < self.max_conditions:
            conds.append(self._random_predicate(
                self.rng.pick(OBJECTIVE_FEATURES[obj])))
        elif r < 0.70 and len(conds) > 1:
            conds.pop(self.rng.u32() % len(conds))
        elif r < 0.85:
            # regime shift: the cheapest way to find an uncovered niche, because
            # it changes WHEN the lens speaks without changing WHAT it says
            reg = self.rng.pick(list(RegimeFilter))
        else:
            hor = self.rng.pick(list(Horizon))

        return LensSpec(self._next_id("M"), tuple(conds), obj, hor, reg,
                        direction, parent.generation + 1, (parent.lens_id,))

    def crossover(self, a: LensSpec, b: LensSpec) -> LensSpec:
        """Recombine two proven parents. Conditions are pooled and truncated;
        the axes are inherited one from each side."""
        pool = list(a.conditions) + list(b.conditions)
        seen, conds = set(), []
        for c in pool:
            if c.feature in seen:
                continue
            seen.add(c.feature)
            conds.append(c)
            if len(conds) >= self.max_conditions:
                break
        obj = a.objective if self.rng.chance(0.5) else b.objective
        hor = a.horizon if self.rng.chance(0.5) else b.horizon
        reg = a.regime if self.rng.chance(0.5) else b.regime
        direction = a.direction_long if self.rng.chance(0.5) else b.direction_long
        return LensSpec(self._next_id("X"), tuple(conds), obj, hor, reg,
                        direction, max(a.generation, b.generation) + 1,
                        (a.lens_id, b.lens_id))

    # ---- targeted (aimed at coverage gaps) -------------------------------

    def targeted(self, objective: Objective, regime: RegimeFilter,
                 horizon: Horizon, generation: int = 0) -> LensSpec:
        """Generate INTO a hole in the roster's coverage rather than at random.
        This is what makes the search efficient: novelty stops being a filter
        you hope to pass and becomes the objective you aim at."""
        return self.random_lens(objective, horizon, regime, generation)


# ---------------------------------------------------------------- LLM

LLM_PROPOSAL_PROMPT = """\
You are a quantitative researcher proposing NEW trading hypotheses for gold
(XAU/USD) microstructure. You may use ONLY these features:

{features}

The engine already runs these lenses. Do NOT propose anything behaviorally
similar to them — a duplicate is worse than nothing, because the ensemble would
treat two clones agreeing as independent confirmation and over-bet exactly where
it is least diversified:

{incumbents}

These market conditions are currently UNCOVERED — no existing lens fires here:

{gaps}

Lessons the engine has already earned from graded outcomes:

{lessons}

Propose {n} hypotheses as a JSON array. Each object:
{{
  "rationale": "<the auction mechanism — WHO is trapped/forced/exhausted, and why
                that produces the move. Not 'RSI is low'. WHY does that matter
                here.>",
  "objective": "reversal|continuation|breakout|failure|liquidity|compression",
  "horizon": "scalp|intraday|swing",
  "regime": "any|trending|balanced|high_vol|low_vol|london|ny|asia",
  "direction": "long|short",
  "conditions": [{{"feature":"<name>","op":">|<|crosses_above|crosses_below|rising|falling","value":<float>}}]
}}

Rules:
  - 1 to 3 conditions. More is overfitting, not sophistication.
  - The direction must be COHERENT with the objective and conditions.
  - Aim at the uncovered conditions. Somewhere already covered is wasted budget.
  - A hypothesis you cannot state a mechanism for is not a hypothesis.

Return ONLY the JSON array."""


def build_llm_prompt(incumbents: Sequence[LensSpec], gaps: Sequence[str],
                     lessons: Sequence[str], n: int = 5) -> str:
    return LLM_PROPOSAL_PROMPT.format(
        features="\n".join(f"  - {k} (typical range {v[0]} .. {v[1]})"
                           for k, v in FEATURES.items()),
        incumbents="\n".join(f"  - {l.describe()}" for l in incumbents[:20])
        or "  (none yet)",
        gaps="\n".join(f"  - {g}" for g in gaps) or "  (no analysis yet)",
        lessons="\n".join(f"  - {l}" for l in lessons[:10]) or "  (none yet)",
        n=n)


def parse_llm_proposals(payload, gen: Generator,
                        generation: int = 0) -> List[Tuple[LensSpec, str]]:
    """Parse model output into LensSpecs. Anything malformed is DROPPED, never
    repaired -- a half-understood hypothesis is not worth a seat."""
    import json
    if isinstance(payload, str):
        s = payload.strip()
        i, j = s.find("["), s.rfind("]")
        if i < 0 or j < 0:
            return []
        try:
            payload = json.loads(s[i:j + 1])
        except Exception:
            return []
    out: List[Tuple[LensSpec, str]] = []
    for item in payload or []:
        try:
            conds = []
            for c in item.get("conditions", [])[:3]:
                feat = str(c["feature"])
                if feat not in FEATURES:
                    raise ValueError(f"unknown feature {feat}")
                conds.append(Predicate(feat, Op(str(c["op"])),
                                       float(c.get("value", 0.0))))
            if not conds:
                continue
            spec = LensSpec(
                gen._next_id("L"), tuple(conds),
                Objective(str(item["objective"])),
                Horizon(str(item["horizon"])),
                RegimeFilter(str(item.get("regime", "any"))),
                str(item.get("direction", "long")).lower() == "long",
                generation)
            out.append((spec, str(item.get("rationale", ""))[:500]))
        except Exception:
            continue
    return out
