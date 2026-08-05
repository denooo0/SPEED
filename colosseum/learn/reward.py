"""The reward function: what the Colosseum actually optimizes.

This is the most consequential file in the learning stack, because whatever it
rewards is what the seats will become. Priorities, in order:

  1. CALIBRATION      -- honest probabilities beat impressive-sounding ones
  2. PATH UNDERSTANDING -- did it know the route, not just the destination
  3. NET R            -- cost-adjusted, never gross
  4. MECHANISM TRUTH  -- explicit penalty for being right for the wrong reason

That fourth term is the anti-laziness clause. Without it, a seat that gets lucky
with a false thesis is indistinguishable from one that understands the market,
and over time the lucky one wins shelf space and the engine gets dumber while
its equity curve looks fine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class RewardWeights:
    calibration: float = 1.0
    direction: float = 0.5
    path: float = 0.8
    risk_adjusted: float = 1.0
    # Calibrated empirically (see tests/test_reward_ordering): must be large
    # enough that a WIN on a false mechanism scores BELOW a reasoned LOSS.
    # At 1.2 it did not, which quietly contradicted the whole anti-laziness
    # premise -- a seat could still farm rating with lucky, unexplained wins.
    mechanism_penalty: float = 1.5
    stale_penalty: float = 2.0
    cost_penalty: float = 0.5
    exploration_bonus: float = 0.15


@dataclass(frozen=True)
class RewardBreakdown:
    total: float
    calibration: float
    direction: float
    path: float
    risk_adjusted: float
    mechanism: float
    penalties: float
    note: str

    def to_dict(self) -> Dict[str, float]:
        return {"total": round(self.total, 4),
                "calibration": round(self.calibration, 4),
                "direction": round(self.direction, 4),
                "path": round(self.path, 4),
                "risk_adjusted": round(self.risk_adjusted, 4),
                "mechanism": round(self.mechanism, 4),
                "penalties": round(self.penalties, 4),
                "note": self.note}


def compute_reward(conviction: float, won: bool, realized_r: float,
                   path_score: float, mechanism_verdict: str,
                   *, was_stale: bool = False, is_exploration: bool = False,
                   cost_r: float = 0.0,
                   w: Optional[RewardWeights] = None) -> RewardBreakdown:
    """One graded prediction -> a scalar the meta-learner can climb.

    `realized_r` MUST already be net of spread/commission/slippage. Feeding gross
    R here teaches the engine to love trades that lose money in the real world.
    """
    w = w or RewardWeights()
    c = min(max(conviction, 0.0), 1.0)
    o = 1.0 if won else 0.0

    # -- 1. Calibration: negatively-oriented Brier, recentred so that a
    #    perfectly-calibrated confident call scores positive and a confident
    #    wrong call is severely negative.
    brier = (c - o) ** 2
    calib = w.calibration * (0.25 - brier) * 4.0     # ~[-3, +1]

    # -- 2. Direction: modest. Being right is table stakes, not the goal.
    direction = w.direction * (1.0 if won else -1.0)

    # -- 3. Path understanding, centred at 0.5 so a coin-flip route is neutral.
    path = w.path * (path_score - 0.5) * 2.0

    # -- 4. Risk-adjusted return, squashed so one lottery win can't dominate a
    #    seat's rating. tanh keeps outliers from rewriting the leaderboard.
    risk = w.risk_adjusted * _tanh(realized_r / 2.0)

    # -- 5. Mechanism verdict. The anti-laziness clause.
    mv = (mechanism_verdict or "").lower()
    if mv == "confirmed":
        mech = w.mechanism_penalty * 0.5
    elif mv == "partial":
        mech = 0.0
    else:                                   # false
        # Being right for a false reason is punished HARDER than a reasoned loss.
        mech = -w.mechanism_penalty * (1.5 if won else 1.0)

    penalties = 0.0
    notes = []
    if was_stale:
        penalties -= w.stale_penalty
        notes.append("stale frame")
    if cost_r > 0:
        penalties -= w.cost_penalty * cost_r
    if is_exploration:
        penalties += w.exploration_bonus
        notes.append("exploration")

    total = calib + direction + path + risk + mech + penalties
    if mv == "false" and won:
        notes.append("RIGHT FOR THE WRONG REASON — punished above the win")
    return RewardBreakdown(total, calib, direction, path, risk, mech, penalties,
                           "; ".join(notes) or "clean")


def _tanh(x: float) -> float:
    if x > 20:
        return 1.0
    if x < -20:
        return -1.0
    e = pow(2.718281828459045, 2 * x)
    return (e - 1) / (e + 1)


def stand_down_reward(was_correct_silence: bool, w: Optional[RewardWeights] = None) -> float:
    """Standing down is a scored ACTION, not an absence of one.

    Correct silence earns a small positive; missing an obvious, high-quality
    setup earns a small negative. Small on purpose -- if silence paid as well as
    a good call, the engine would learn to never trade; if it paid nothing, the
    engine would learn to force signals. Both failure modes are worse than the
    calibration cost of getting this weight roughly right.
    """
    return 0.10 if was_correct_silence else -0.15
