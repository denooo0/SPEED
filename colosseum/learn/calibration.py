"""Calibration: the primary scoring axis.

The engine's cardinal rule is that an honest 55% beats a dishonest 90%. This
module is where that rule gets teeth.

Brier score decomposes as:

    Brier = Reliability - Resolution + Uncertainty

  * Reliability (lower better) -- do 70% calls actually happen 70% of the time?
  * Resolution  (higher better) -- does the model discriminate at all, or does
    it just predict the base rate forever?
  * Uncertainty -- irreducible, a property of the market not the model.

Tracking the decomposition rather than raw Brier is what stops a seat from
gaming the metric by predicting 50% on everything: that scores a fine
Reliability but zero Resolution, and the reward function sees it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class Bin:
    """`n` is a float: with recency decay it is an EFFECTIVE sample count, not
    an integer tally. Treating it as an int is what silently froze the
    reliability curve."""
    lo: float
    hi: float
    n: float = 0.0
    sum_pred: float = 0.0
    sum_out: float = 0.0

    @property
    def mean_pred(self) -> float:
        return self.sum_pred / self.n if self.n else 0.0

    @property
    def observed(self) -> float:
        return self.sum_out / self.n if self.n else 0.0

    @property
    def gap(self) -> float:
        return self.observed - self.mean_pred


class Calibrator:
    """Streaming reliability tracking with optional recency decay.

    Decay matters: gold in 2023 is not gold now. A calibration estimate that
    weights a two-year-old prediction equally with yesterday's will always be
    late to a regime change.
    """

    def __init__(self, n_bins: int = 10, half_life: Optional[float] = 500.0):
        self.bins = [Bin(i / n_bins, (i + 1) / n_bins) for i in range(n_bins)]
        self.n = 0
        self._sq_err = 0.0
        self._sum_out = 0.0
        self._w = 0.0
        self.half_life = half_life
        self._decay = (0.5 ** (1.0 / half_life)) if half_life else 1.0

    def update(self, pred: float, outcome: int) -> None:
        """pred in [0,1]; outcome in {0,1}."""
        p = min(max(pred, 0.0), 1.0)
        o = 1 if outcome else 0
        if self._decay < 1.0:
            # Decay the BINS as well as the aggregates. Previously only the
            # aggregates decayed, so `brier` and `decomposition()["brier"]`
            # disagreed (0.64 vs 0.34 across a regime flip) and the reliability
            # curve was an all-time average that could never show a seat going
            # bad. A calibration tracker that cannot see a regime change is
            # worse than none -- it reports false reassurance.
            for b in self.bins:
                if b.n:
                    b.n *= self._decay
                    b.sum_pred *= self._decay
                    b.sum_out *= self._decay
            self._sq_err *= self._decay
            self._sum_out *= self._decay
            self._w *= self._decay
        idx = min(int(p * len(self.bins)), len(self.bins) - 1)
        b = self.bins[idx]
        b.n += 1
        b.sum_pred += p
        b.sum_out += o
        self._sq_err += (p - o) ** 2
        self._sum_out += o
        self._w += 1.0
        self.n += 1

    @property
    def brier(self) -> float:
        return self._sq_err / self._w if self._w > 0 else 0.25

    @property
    def base_rate(self) -> float:
        return self._sum_out / self._w if self._w > 0 else 0.5

    def decomposition(self) -> Dict[str, float]:
        total = sum(b.n for b in self.bins)
        if total == 0:
            return {"reliability": 0.0, "resolution": 0.0,
                    "uncertainty": 0.25, "brier": 0.25}
        base = sum(b.sum_out for b in self.bins) / total
        rel = sum(b.n * (b.mean_pred - b.observed) ** 2
                  for b in self.bins if b.n) / total
        res = sum(b.n * (b.observed - base) ** 2
                  for b in self.bins if b.n) / total
        unc = base * (1 - base)
        return {"reliability": rel, "resolution": res, "uncertainty": unc,
                "brier": rel - res + unc, "base_rate": base}

    def overconfidence(self) -> float:
        """>0 means the seat claims more than it delivers. This is the number
        that gets a seat relegated even while its win rate looks fine."""
        tot = sum(b.n for b in self.bins)
        if not tot:
            return 0.0
        return sum(b.n * (b.mean_pred - b.observed) for b in self.bins if b.n) / tot

    def reliability_curve(self) -> List[Tuple[float, float, float]]:
        return [(b.mean_pred, b.observed, round(b.n, 2))
                for b in self.bins if b.n > 1e-6]

    def report(self) -> Dict[str, float]:
        d = self.decomposition()
        d.update({"n": self.n, "overconfidence": self.overconfidence()})
        return d


class IsotonicCalibrator:
    """Pool-Adjacent-Violators isotonic regression.

    When a seat is systematically overconfident, this maps its raw conviction to
    an empirically-honest probability WITHOUT retraining the seat. It's the fast,
    safe repair rung for calibration drift -- Guardian's `rollback_params` leans
    on it.
    """

    def __init__(self) -> None:
        self.x: List[float] = []
        self.y: List[float] = []
        self._fitted = False

    def fit(self, preds: Sequence[float], outcomes: Sequence[int]) -> "IsotonicCalibrator":
        pairs = sorted(zip(preds, outcomes))
        if not pairs:
            return self
        xs = [p for p, _ in pairs]
        ys = [float(o) for _, o in pairs]
        w = [1.0] * len(ys)
        i = 0
        while i < len(ys) - 1:
            if ys[i] <= ys[i + 1]:
                i += 1
                continue
            tw = w[i] + w[i + 1]
            ty = (ys[i] * w[i] + ys[i + 1] * w[i + 1]) / tw
            ys[i:i + 2] = [ty]
            w[i:i + 2] = [tw]
            xs[i:i + 2] = [xs[i]]
            i = max(i - 1, 0)
        self.x, self.y, self._fitted = xs, ys, True
        return self

    def transform(self, p: float) -> float:
        if not self._fitted or not self.x:
            return p
        if p <= self.x[0]:
            return self.y[0]
        if p >= self.x[-1]:
            return self.y[-1]
        lo, hi = 0, len(self.x) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.x[mid] <= p:
                lo = mid
            else:
                hi = mid
        x0, x1 = self.x[lo], self.x[hi]
        y0, y1 = self.y[lo], self.y[hi]
        if x1 == x0:
            return y0
        return y0 + (y1 - y0) * (p - x0) / (x1 - x0)


def log_loss(preds: Sequence[float], outcomes: Sequence[int],
             eps: float = 1e-12) -> float:
    """Punishes confident-wrong far harder than uncertain-wrong -- exactly the
    asymmetry the engine wants, since overconfidence is the failure mode that
    most corrupts the learner."""
    if not preds:
        return 0.0
    s = 0.0
    for p, o in zip(preds, outcomes):
        p = min(max(p, eps), 1 - eps)
        s += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return s / len(preds)
