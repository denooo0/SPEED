"""Statistical gates against backtest overfitting.

THIS IS THE MOST IMPORTANT MODULE IN THE FOUNDRY. Without it, automated lens
generation is a machine for manufacturing confident garbage.

The problem, stated precisely: if you generate 10,000 candidate lenses and keep
the best, the best one's Sharpe ratio is an ORDER STATISTIC, not an estimate. Its
expected value is high even when every single candidate is pure noise. The
maximum of 10,000 draws from a zero-mean distribution looks spectacular. This is
why most published/marketed strategies fail live: nobody deflates for the number
of trials.

Four defenses, all from Bailey & López de Prado:

  DEFLATED SHARPE RATIO (DSR)
      Adjusts the observed Sharpe for (a) the number of independent trials,
      (b) skew and kurtosis of returns, (c) sample length. Answers: "what is
      the probability this Sharpe is genuinely > 0, given I looked at N
      strategies?"

  PROBABILITY OF BACKTEST OVERFITTING (PBO) via CSCV
      Split the return series into S blocks, take every balanced combination of
      train/test, and ask: how often does the IS-best configuration rank in the
      bottom half OOS? If that happens >50% of the time you are just fitting
      noise. Elegantly, it needs no distributional assumptions.

  MINIMUM BACKTEST LENGTH (MinBTL)
      Given N trials, how many years of data do you NEED before an observed
      Sharpe of a given size is credible? Usually a sobering number.

  HAIRCUT
      What Sharpe would survive after deflation? Report the haircut so the
      degradation is visible rather than implied.

If a candidate lens cannot clear these gates it does not get a seat. No
exceptions, no "but the equity curve looks so good."
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Sequence, Tuple

SQRT_2PI = math.sqrt(2.0 * math.pi)
EULER = 0.5772156649015329


# ---------------------------------------------------------------- basics

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, ~1e-9 accurate)."""
    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


@dataclass(frozen=True)
class Moments:
    n: int
    mean: float
    std: float
    skew: float
    kurtosis: float          # NON-excess (normal = 3)

    @property
    def sharpe(self) -> float:
        return self.mean / self.std if self.std > 0 else 0.0


def moments(returns: Sequence[float]) -> Moments:
    n = len(returns)
    if n < 3:
        return Moments(n, 0.0, 0.0, 0.0, 3.0)
    m = sum(returns) / n
    var = sum((r - m) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd <= 0:
        return Moments(n, m, 0.0, 0.0, 3.0)
    z = [(r - m) / sd for r in returns]
    skew = sum(x ** 3 for x in z) / n
    kurt = sum(x ** 4 for x in z) / n
    return Moments(n, m, sd, skew, kurt)


# ---------------------------------------------------------------- DSR

def expected_max_sharpe(n_trials: int, var_sharpe: float = 1.0) -> float:
    """E[max Sharpe] across N INDEPENDENT trials of zero-mean strategies.

    This is the benchmark the observed Sharpe must beat. The intuition worth
    internalising: with 1,000 worthless strategies, the best one still shows a
    Sharpe near 3.2 sigma. That is not skill. That is the maximum of 1,000 draws.
    """
    if n_trials < 2:
        return 0.0
    sd = math.sqrt(max(var_sharpe, 1e-12))
    a = _norm_ppf(1.0 - 1.0 / n_trials)
    b = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return sd * ((1.0 - EULER) * a + EULER * b)


def probabilistic_sharpe(sr: float, benchmark: float, m: Moments) -> float:
    """P(true Sharpe > benchmark), correcting for skew and fat tails.

    Negative skew and high kurtosis INFLATE the naive Sharpe -- exactly the
    return profile of "sell the wing, collect premium, blow up occasionally."
    This is where such strategies get properly penalised.
    """
    if m.n < 3 or m.std <= 0:
        return 0.5
    num = (sr - benchmark) * math.sqrt(m.n - 1)
    den = math.sqrt(max(1e-12,
                        1.0 - m.skew * sr + ((m.kurtosis - 1.0) / 4.0) * sr * sr))
    return _norm_cdf(num / den)


@dataclass(frozen=True)
class DSRResult:
    sharpe: float
    deflated_sharpe: float       # P(true SR > E[max SR under the null])
    expected_max: float
    n_trials: int
    haircut: float               # fraction of Sharpe destroyed by deflation
    passes: bool
    verdict: str

    def to_dict(self) -> Dict[str, object]:
        return {"sharpe": round(self.sharpe, 4),
                "deflated_sharpe": round(self.deflated_sharpe, 4),
                "expected_max_under_null": round(self.expected_max, 4),
                "n_trials": self.n_trials,
                "haircut": round(self.haircut, 4),
                "passes": self.passes, "verdict": self.verdict}


def deflated_sharpe(returns: Sequence[float], n_trials: int,
                    var_sharpe: Optional[float] = None,
                    threshold: float = 0.95) -> DSRResult:
    """The gate. `n_trials` MUST be the true number of candidates evaluated.

    Understating n_trials is self-deception with extra steps -- the whole point
    is to pay for every look you took.
    """
    m = moments(returns)
    sr = m.sharpe
    if m.n < 20 or m.std <= 0:
        return DSRResult(sr, 0.0, 0.0, n_trials, 1.0, False,
                         f"insufficient sample ({m.n} returns) to make any claim")

    vs = var_sharpe if var_sharpe is not None else 1.0 / max(m.n - 1, 1)
    emax = expected_max_sharpe(n_trials, vs)
    dsr = probabilistic_sharpe(sr, emax, m)
    haircut = 1.0 - (max(sr - emax, 0.0) / sr) if sr > 0 else 1.0
    passes = dsr >= threshold

    if passes:
        verdict = (f"SURVIVES deflation: SR {sr:.3f} beats the {emax:.3f} "
                   f"expected from the best of {n_trials} worthless trials, "
                   f"with {dsr:.1%} confidence")
    elif sr <= emax:
        verdict = (f"REJECT: SR {sr:.3f} is at or below {emax:.3f} — the Sharpe "
                   f"you would expect from the luckiest of {n_trials} random "
                   f"strategies. This is an order statistic, not an edge.")
    else:
        verdict = (f"REJECT: SR {sr:.3f} exceeds the {emax:.3f} null but only at "
                   f"{dsr:.1%} confidence (need {threshold:.0%}). "
                   f"{'Negative skew is inflating the raw Sharpe. ' if m.skew < -0.5 else ''}"
                   f"Not enough evidence.")
    return DSRResult(sr, dsr, emax, n_trials, haircut, passes, verdict)


# ---------------------------------------------------------------- PBO

@dataclass(frozen=True)
class PBOResult:
    pbo: float                   # P(the IS-best config underperforms OOS median)
    n_splits: int
    median_oos_rank: float
    logit_slope: float           # IS->OOS performance degradation
    passes: bool
    verdict: str

    def to_dict(self) -> Dict[str, object]:
        return {"pbo": round(self.pbo, 4), "n_splits": self.n_splits,
                "median_oos_rank": round(self.median_oos_rank, 4),
                "logit_slope": round(self.logit_slope, 4),
                "passes": self.passes, "verdict": self.verdict}


def probability_of_backtest_overfitting(
        matrix: Sequence[Sequence[float]], s_blocks: int = 8,
        threshold: float = 0.35) -> PBOResult:
    """CSCV. `matrix[i][t]` = return of strategy i in period t.

    Split time into S blocks; over every balanced train/test partition, select
    the IS winner and see where it lands OOS. PBO is the fraction of partitions
    where the IS winner falls below the OOS median.

    PBO > 0.5 literally means your selection procedure is worse than random --
    picking the in-sample best is actively anti-predictive. This catches
    overfitting that DSR misses, because it tests the SELECTION PROCESS rather
    than any single strategy.
    """
    n_strat = len(matrix)
    if n_strat < 2:
        return PBOResult(0.0, 0, 0.5, 0.0, False,
                         "need at least 2 candidate strategies to assess "
                         "selection overfitting")
    T = min(len(r) for r in matrix)
    if T < s_blocks * 4:
        return PBOResult(1.0, 0, 0.5, 0.0, False,
                         f"sample too short ({T}) for {s_blocks}-block CSCV")
    if s_blocks % 2:
        s_blocks -= 1

    size = T // s_blocks
    blocks = [list(range(b * size, (b + 1) * size)) for b in range(s_blocks)]

    def _sr(idx: Sequence[int], row: Sequence[float]) -> float:
        vals = [row[i] for i in idx]
        n = len(vals)
        if n < 2:
            return 0.0
        mu = sum(vals) / n
        var = sum((v - mu) ** 2 for v in vals) / (n - 1)
        return mu / math.sqrt(var) if var > 0 else 0.0

    logits: List[float] = []
    ranks: List[float] = []
    half = s_blocks // 2
    combos = list(combinations(range(s_blocks), half))
    # cap the work: CSCV is combinatorial and we do not need all of it
    if len(combos) > 200:
        step = len(combos) // 200
        combos = combos[::step][:200]

    for train_blocks in combos:
        tr_idx = [i for b in train_blocks for i in blocks[b]]
        te_idx = [i for b in range(s_blocks) if b not in train_blocks
                  for i in blocks[b]]
        is_perf = [_sr(tr_idx, r) for r in matrix]
        oos_perf = [_sr(te_idx, r) for r in matrix]
        best = max(range(n_strat), key=lambda i: is_perf[i])

        order = sorted(range(n_strat), key=lambda i: oos_perf[i])
        rank = order.index(best) / max(n_strat - 1, 1)   # 0 worst .. 1 best
        ranks.append(rank)
        w = min(max(rank, 1e-6), 1 - 1e-6)
        logits.append(math.log(w / (1 - w)))

    if not logits:
        return PBOResult(1.0, 0, 0.5, 0.0, False, "no valid CSCV partitions")

    pbo = sum(1 for l in logits if l <= 0) / len(logits)
    med_rank = sorted(ranks)[len(ranks) // 2]
    slope = sum(logits) / len(logits)
    passes = pbo <= threshold

    if passes:
        verdict = (f"PBO {pbo:.1%}: the in-sample winner usually stays good "
                   f"out-of-sample (median OOS rank {med_rank:.0%}). The "
                   f"selection process is picking signal, not noise.")
    elif pbo > 0.5:
        verdict = (f"PBO {pbo:.1%} — WORSE THAN RANDOM. Selecting the in-sample "
                   f"best is actively anti-predictive here. The candidate pool "
                   f"is noise and the search is memorising it.")
    else:
        verdict = (f"PBO {pbo:.1%} exceeds the {threshold:.0%} limit. Too often "
                   f"the in-sample winner is a below-median performer OOS.")
    return PBOResult(pbo, len(logits), med_rank, slope, passes, verdict)


# ---------------------------------------------------------------- MinBTL

def minimum_backtest_length(n_trials: int, target_sharpe: float = 1.0,
                            annual_periods: int = 252) -> Dict[str, float]:
    """How much data do you NEED before a claimed Sharpe is credible?

    Reality check that stops a lot of bad projects early: to justify a Sharpe of
    1.0 after searching 1,000 configurations you need a genuinely large sample.
    If you do not have it, the honest move is to search less, not to believe more.
    """
    if n_trials < 2 or target_sharpe <= 0:
        return {"years": 0.0, "periods": 0.0, "n_trials": n_trials}
    e = expected_max_sharpe(n_trials, 1.0)
    years = (e / target_sharpe) ** 2
    return {"years": round(years, 3),
            "periods": round(years * annual_periods, 1),
            "expected_max_sharpe_under_null": round(e, 4),
            "n_trials": n_trials}


# ---------------------------------------------------------------- combined

@dataclass(frozen=True)
class ValidationVerdict:
    approved: bool
    dsr: DSRResult
    pbo: Optional[PBOResult]
    n_trials: int
    reasons: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return {"approved": self.approved, "n_trials": self.n_trials,
                "dsr": self.dsr.to_dict(),
                "pbo": self.pbo.to_dict() if self.pbo else None,
                "reasons": list(self.reasons)}


def validate_candidate(returns: Sequence[float], n_trials: int,
                       peer_matrix: Optional[Sequence[Sequence[float]]] = None,
                       dsr_threshold: float = 0.95,
                       pbo_threshold: float = 0.35,
                       min_returns: int = 60) -> ValidationVerdict:
    """The full gate a generated lens must pass before it can take a seat."""
    reasons: List[str] = []
    d = deflated_sharpe(returns, n_trials, threshold=dsr_threshold)
    if len(returns) < min_returns:
        reasons.append(f"only {len(returns)} trades; need {min_returns}+ before "
                       f"any statistical claim is meaningful")
    if not d.passes:
        reasons.append(d.verdict)

    p = None
    if peer_matrix and len(peer_matrix) >= 2:
        p = probability_of_backtest_overfitting(peer_matrix,
                                                threshold=pbo_threshold)
        if not p.passes:
            reasons.append(p.verdict)

    approved = not reasons
    if approved:
        reasons.append(d.verdict)
    return ValidationVerdict(approved, d, p, n_trials, tuple(reasons))
