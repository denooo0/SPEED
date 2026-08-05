"""Colosseum ranking: promotion, relegation, and anti-collusion.

Standard Elo compares two players on one game. Here every seat plays the same
opponent -- the market -- so the tournament is scored differently: each graded
prediction is a "match" against a difficulty-adjusted expectation, and a seat
gains rating only by beating what a competent seat should have achieved on that
same setup.

Three properties that keep the arena honest:

  * CALIBRATION-WEIGHTED. A seat with a great win rate and dishonest conviction
    loses rating. The scoreboard punishes miscalibration harder than silence.
  * ANTI-COLLUSION. Agreeing with consensus and being right pays less than being
    distinctly right. Without this the three seats converge into one voice and
    the whole competitive premise becomes theatre.
  * DECAYING. Old glory fades, so a seat must keep earning its shelf space.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SeatRating:
    seat_id: str
    lens: str
    elo: float = 1500.0
    n: int = 0
    wins: int = 0
    sum_reward: float = 0.0
    sum_r: float = 0.0
    brier_sum: float = 0.0
    mechanism_confirmed: int = 0
    distinct_correct: int = 0
    consensus_correct: int = 0
    status: str = "active"          # active | probation | benched | retired
    benched_until_n: int = 0
    peak_elo: float = 1500.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def avg_reward(self) -> float:
        return self.sum_reward / self.n if self.n else 0.0

    @property
    def avg_r(self) -> float:
        return self.sum_r / self.n if self.n else 0.0

    @property
    def brier(self) -> float:
        return self.brier_sum / self.n if self.n else 0.25

    @property
    def mechanism_rate(self) -> float:
        return self.mechanism_confirmed / self.n if self.n else 0.0

    @property
    def distinctness(self) -> float:
        """Share of correct calls that were NOT consensus echoes."""
        tot = self.distinct_correct + self.consensus_correct
        return self.distinct_correct / tot if tot else 0.0

    def to_dict(self) -> Dict[str, object]:
        return {"seat_id": self.seat_id, "lens": self.lens,
                "elo": round(self.elo, 1), "n": self.n,
                "win_rate": round(self.win_rate, 4),
                "avg_reward": round(self.avg_reward, 4),
                "avg_r": round(self.avg_r, 4), "brier": round(self.brier, 4),
                "mechanism_rate": round(self.mechanism_rate, 4),
                "distinctness": round(self.distinctness, 4),
                "status": self.status, "peak_elo": round(self.peak_elo, 1)}


class Colosseum:
    """The league table. Owns shelf space."""

    def __init__(self, k: float = 24.0, decay: float = 0.999,
                 bench_elo: float = 1380.0, restore_elo: float = 1470.0,
                 min_n_for_bench: int = 30):
        self.seats: Dict[str, SeatRating] = {}
        self.k = k
        self.decay = decay
        self.bench_elo = bench_elo
        self.restore_elo = restore_elo
        self.min_n_for_bench = min_n_for_bench
        self.events: List[Dict[str, object]] = []

    def add_seat(self, seat_id: str, lens: str, elo: float = 1500.0) -> SeatRating:
        s = SeatRating(seat_id=seat_id, lens=lens, elo=elo, peak_elo=elo)
        self.seats[seat_id] = s
        return s

    def record(self, seat_id: str, *, conviction: float, won: bool,
               reward: float, realized_r: float, mechanism_verdict: str,
               was_consensus: bool = False) -> SeatRating:
        s = self.seats[seat_id]
        s.n += 1
        s.wins += 1 if won else 0
        s.sum_reward += reward
        s.sum_r += realized_r
        s.brier_sum += (conviction - (1.0 if won else 0.0)) ** 2
        if mechanism_verdict == "confirmed":
            s.mechanism_confirmed += 1
        if won:
            if was_consensus:
                s.consensus_correct += 1
            else:
                s.distinct_correct += 1

        # Expected score from the seat's own claim: a 0.8-conviction call is
        # SUPPOSED to win. Winning it is unremarkable; losing it is expensive.
        expected = min(max(conviction, 0.01), 0.99)
        actual = 1.0 if won else 0.0

        # Calibration multiplier: reward honest confidence, punish inflation.
        calib_mult = 1.0 + (0.25 - (conviction - actual) ** 2) * 1.5

        # Anti-collusion: distinct correct calls are worth more than echoes.
        distinct_mult = 1.0 if not won else (0.75 if was_consensus else 1.25)

        # Mechanism truth gate: a win on a false thesis cannot raise rating.
        if mechanism_verdict == "false":
            mech_mult = -0.5 if won else 0.75
        elif mechanism_verdict == "partial":
            mech_mult = 0.75
        else:
            mech_mult = 1.0

        delta = self.k * (actual - expected) * calib_mult * distinct_mult * mech_mult
        delta += 0.5 * reward          # the reward function's own opinion

        s.elo = s.elo * self.decay + (1 - self.decay) * 1500.0 + delta
        s.peak_elo = max(s.peak_elo, s.elo)
        self._reassess(s)
        return s

    def _reassess(self, s: SeatRating) -> None:
        prev = s.status
        if s.status == "benched":
            if s.elo >= self.restore_elo:
                s.status = "probation"
        elif s.n >= self.min_n_for_bench and s.elo < self.bench_elo:
            # Benched != deleted. It keeps predicting on paper so it can still
            # learn and re-qualify. Removing it entirely would lose the diversity
            # that makes the arena work.
            s.status = "benched"
            s.benched_until_n = s.n + 25
        elif s.status == "probation" and s.n >= s.benched_until_n:
            s.status = "active"
        if s.status != prev:
            self.events.append({"seat_id": s.seat_id, "from": prev,
                                "to": s.status, "elo": round(s.elo, 1), "n": s.n})

    # ---- shelf space -----------------------------------------------------

    def table(self) -> List[SeatRating]:
        return sorted(self.seats.values(), key=lambda s: -s.elo)

    def rank_of(self, seat_id: str) -> int:
        return next(i + 1 for i, s in enumerate(self.table()) if s.seat_id == seat_id)

    def shelf_weight(self, seat_id: str) -> float:
        """How much influence this seat has on published output.

        Benched seats get exactly zero live weight but keep running on paper.
        """
        s = self.seats[seat_id]
        if s.status in ("benched", "retired"):
            return 0.0
        if s.status == "probation":
            return 0.35
        active = [x for x in self.table() if x.status == "active"]
        if not active:
            return 0.0
        # Softmax over Elo, temperature 80 -- keeps the #2 seat meaningful
        # instead of winner-take-all, which would kill diversity.
        mx = max(x.elo for x in active)
        exps = {x.seat_id: math.exp((x.elo - mx) / 80.0) for x in active}
        tot = sum(exps.values())
        return exps.get(seat_id, 0.0) / tot if tot else 0.0

    def inter_seat_correlation(self, agreement_matrix: Dict[str, Dict[str, float]]) -> float:
        """Max pairwise agreement. Fed to the consensus_collapse detector: if the
        seats stop disagreeing, the Colosseum has quietly become one model."""
        vals = [v for a, row in agreement_matrix.items()
                for b, v in row.items() if a != b]
        return max(vals) if vals else 0.0

    def scoreboard(self) -> List[Dict[str, object]]:
        out = []
        for i, s in enumerate(self.table()):
            d = s.to_dict()
            d["rank"] = i + 1
            d["shelf_weight"] = round(self.shelf_weight(s.seat_id), 4)
            out.append(d)
        return out
