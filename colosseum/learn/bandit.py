"""Contextual bandit: learning where stops and targets actually belong.

This is the "readjust SL/TP/entry/exit over time" requirement.

Design constraint that keeps it safe: the bandit does NOT invent price levels.
It selects a MULTIPLIER applied to a structurally-derived level. Structure says
"the stop belongs above that swing high"; the bandit learns "in high-vol NY,
1.3x the ATR buffer beyond it survives noise better than 1.0x." Structure owns
the anchor; the bandit owns the tuning. A bandit allowed to place stops directly
would eventually put one in mid-air with a great backtest justification.

LinUCB (disjoint) is used because it gives principled optimism under uncertainty
-- it explores parameter arms it hasn't tried in a given regime *because* it
hasn't tried them, then stops once it knows. Uncertainty-aware exploration beats
epsilon-greedy here, since regimes are the context and some are rare.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ParamArm:
    """A candidate tuning. Multipliers on structure-derived levels, never
    absolute prices."""
    arm_id: str
    stop_atr_mult: float      # buffer beyond the structural level, in ATR
    tp1_r: float              # TP1 in R multiples
    tp2_r: float
    entry_offset_atr: float   # patience: wait this far for a better fill
    horizon_mult: float = 1.0

    def to_dict(self) -> Dict[str, float]:
        return {"arm_id": self.arm_id, "stop_atr_mult": self.stop_atr_mult,
                "tp1_r": self.tp1_r, "tp2_r": self.tp2_r,
                "entry_offset_atr": self.entry_offset_atr,
                "horizon_mult": self.horizon_mult}


DEFAULT_ARMS: Tuple[ParamArm, ...] = (
    ParamArm("tight_fast",   0.25, 1.0, 1.8, 0.00, 0.7),
    ParamArm("tight_runner", 0.25, 1.5, 3.0, 0.00, 1.2),
    ParamArm("std",          0.50, 1.5, 2.5, 0.10, 1.0),
    ParamArm("std_runner",   0.50, 2.0, 4.0, 0.10, 1.4),
    ParamArm("wide",         0.90, 1.5, 2.5, 0.25, 1.1),
    ParamArm("wide_runner",  0.90, 2.5, 5.0, 0.25, 1.6),
    ParamArm("patient",      0.60, 2.0, 3.5, 0.45, 1.3),
)


def context_vector(regime: Dict[str, object], conviction: float,
                   spread_atr: float = 0.0) -> List[float]:
    """Regime -> feature vector. One-hot for categoricals plus a bias term.

    Keeping this small and interpretable is deliberate: a 200-dim context needs
    far more data than a live engine accumulates in a useful timeframe, and an
    under-determined bandit is just an expensive random number generator.
    """
    vol = str(regime.get("vol_band", "unknown"))
    trend = str(regime.get("trend_htf", "unknown"))
    sess = str(regime.get("liquidity_session", "unknown"))
    return [
        1.0,
        1.0 if vol == "low" else 0.0,
        1.0 if vol == "normal" else 0.0,
        1.0 if vol == "high" else 0.0,
        1.0 if trend == "up" else 0.0,
        1.0 if trend == "down" else 0.0,
        1.0 if trend == "range" else 0.0,
        1.0 if sess in ("LONDON", "NY_OVERLAP") else 0.0,
        1.0 if sess == "ASIA" else 0.0,
        min(max(conviction, 0.0), 1.0),
        min(spread_atr, 2.0),
    ]


class _ArmModel:
    """Ridge regression with a running inverse via Sherman-Morrison -- O(d^2)
    per update instead of an O(d^3) inversion. At d=11 either is cheap, but the
    incremental form is what lets this run per-signal forever without a batch job."""

    __slots__ = ("d", "A_inv", "b", "n", "reward_sum")

    def __init__(self, d: int, lam: float = 1.0):
        self.d = d
        self.A_inv = [[(1.0 / lam if i == j else 0.0) for j in range(d)]
                      for i in range(d)]
        self.b = [0.0] * d
        self.n = 0
        self.reward_sum = 0.0

    def theta(self) -> List[float]:
        return [sum(self.A_inv[i][j] * self.b[j] for j in range(self.d))
                for i in range(self.d)]

    def predict(self, x: Sequence[float], alpha: float) -> Tuple[float, float]:
        th = self.theta()
        mean = sum(th[i] * x[i] for i in range(self.d))
        Ax = [sum(self.A_inv[i][j] * x[j] for j in range(self.d))
              for i in range(self.d)]
        var = max(0.0, sum(x[i] * Ax[i] for i in range(self.d)))
        return mean, alpha * math.sqrt(var)

    def update(self, x: Sequence[float], reward: float) -> None:
        Ax = [sum(self.A_inv[i][j] * x[j] for j in range(self.d))
              for i in range(self.d)]
        denom = 1.0 + sum(x[i] * Ax[i] for i in range(self.d))
        for i in range(self.d):
            for j in range(self.d):
                self.A_inv[i][j] -= (Ax[i] * Ax[j]) / denom
        for i in range(self.d):
            self.b[i] += reward * x[i]
        self.n += 1
        self.reward_sum += reward


class ParameterBandit:
    def __init__(self, arms: Sequence[ParamArm] = DEFAULT_ARMS,
                 alpha: float = 0.6, dim: int = 11,
                 exploration_rate: float = 0.07, seed: int = 12345):
        self.arms = list(arms)
        self.alpha = alpha
        self.dim = dim
        self.models = {a.arm_id: _ArmModel(dim) for a in self.arms}
        self.exploration_rate = exploration_rate
        self._rng_state = seed          # seeded + logged: replay-deterministic
        self.pulls: Dict[str, int] = {a.arm_id: 0 for a in self.arms}

    def _rand(self) -> float:
        """xorshift32. Deterministic across machines and Python versions --
        random.random() is not guaranteed stable, and replay demands it."""
        x = self._rng_state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17)
        x ^= (x << 5) & 0xFFFFFFFF
        self._rng_state = x & 0xFFFFFFFF
        return self._rng_state / 0xFFFFFFFF

    def select(self, ctx: Sequence[float]) -> Tuple[ParamArm, Dict[str, object]]:
        """Returns (arm, decision_trace). The trace goes into the journal so a
        parameter choice is as auditable as a trade thesis."""
        forced = self._rand() < self.exploration_rate
        if forced:
            arm = self.arms[int(self._rand() * len(self.arms)) % len(self.arms)]
            self.pulls[arm.arm_id] += 1
            return arm, {"mode": "exploration", "arm": arm.arm_id,
                         "reason": "deliberate exploration budget -- without it "
                                   "the learner plateaus at a local optimum"}

        scored = []
        for a in self.arms:
            mean, bonus = self.models[a.arm_id].predict(ctx, self.alpha)
            scored.append((mean + bonus, mean, bonus, a))
        scored.sort(key=lambda t: -t[0])
        ucb, mean, bonus, best = scored[0]
        self.pulls[best.arm_id] += 1
        return best, {
            "mode": "exploit", "arm": best.arm_id,
            "ucb": round(ucb, 4), "predicted_reward": round(mean, 4),
            "uncertainty_bonus": round(bonus, 4),
            "runner_up": scored[1][3].arm_id if len(scored) > 1 else None,
            "margin": round(ucb - scored[1][0], 4) if len(scored) > 1 else None,
            "reason": "highest upper-confidence-bound reward for this regime",
        }

    def update(self, arm_id: str, ctx: Sequence[float], reward: float) -> None:
        if arm_id in self.models:
            self.models[arm_id].update(ctx, reward)

    def snapshot(self) -> Dict[str, object]:
        """Serializable state for the Checkpoint Registry -- this is what
        `rollback_params` restores when calibration collapses."""
        return {"rng_state": self._rng_state, "pulls": dict(self.pulls),
                "arms": {a.arm_id: {"n": self.models[a.arm_id].n,
                                    "avg_reward": round(
                                        self.models[a.arm_id].reward_sum /
                                        self.models[a.arm_id].n, 4)
                                    if self.models[a.arm_id].n else 0.0,
                                    "theta": [round(v, 6) for v in
                                              self.models[a.arm_id].theta()]}
                         for a in self.arms}}

    def restore(self, snap: Dict[str, object]) -> None:
        self._rng_state = int(snap.get("rng_state", self._rng_state))
        self.pulls.update(snap.get("pulls", {}))   # theta rebuilt by replay

    def leaderboard(self) -> List[Dict[str, object]]:
        rows = []
        for a in self.arms:
            m = self.models[a.arm_id]
            rows.append({**a.to_dict(), "pulls": m.n,
                         "avg_reward": round(m.reward_sum / m.n, 4) if m.n else 0.0})
        return sorted(rows, key=lambda r: -r["avg_reward"])
