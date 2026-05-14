"""Backtest performance metrics.

Implements:
    * Sharpe ratio with annualization
    * Sortino ratio
    * Maximum drawdown (peak-to-trough)
    * Calmar ratio (CAGR / |MaxDD|)
    * Deflated Sharpe Ratio (Bailey & López de Prado, 2014)
        DSR = Phi( ( (SR_obs - SR_0) * sqrt(N-1) ) /
                   sqrt(1 - skew*SR_obs + (kurt-1)/4 * SR_obs^2) )
        where SR_0 is the expected max Sharpe under H0 across `n_trials` random
        candidates, derived from the expected-maximum-of-Gaussians approximation
        sqrt(2 * ln(n_trials)).
    * Probability of Backtest Overfitting (PBO) via Combinatorially Symmetric CV
      (Bailey, Borwein, López de Prado, Zhu 2014):
        For each CSCV split, rank IS Sharpes, pick the argmax, then compute the
        rank of THAT model in the OOS split; record the logit. PBO is the
        fraction of logits <= 0 (median model ends below the median OOS).
    * Ruin probability via classic gambler's-ruin Monte-Carlo approximation:
      simulates `n_paths` random walks of length `n_trades` with mean R and
      std R per trade (scaled by risk_per_trade_pct), returns fraction of paths
      that hit -dd_threshold_pct of equity.

All functions are pure and side-effect free.
"""
from __future__ import annotations

import math
from typing import Dict

import numpy as np
import pandas as pd
from scipy import stats


# ---------------------------------------------------------------- helpers


def _safe_periods_per_year_from_bar_seconds(bar_seconds: int) -> int:
    """How many bars in a calendar year (crypto: 24/7)."""
    return max(1, int(round(365 * 24 * 3600 / max(1, int(bar_seconds)))))


# ---------------------------------------------------------------- Sharpe


def compute_sharpe(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Annualized Sharpe ratio of a per-period return series.

    Uses sample std (ddof=1). Returns 0.0 if std is 0 or input is empty.
    """
    if returns is None:
        return 0.0
    r = pd.Series(returns).dropna()
    if r.empty or r.std(ddof=1) == 0 or pd.isna(r.std(ddof=1)):
        return 0.0
    return float(r.mean() / r.std(ddof=1) * math.sqrt(periods_per_year))


# ---------------------------------------------------------------- Sortino


def compute_sortino(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Annualized Sortino ratio (uses downside-only std)."""
    if returns is None:
        return 0.0
    r = pd.Series(returns).dropna()
    if r.empty:
        return 0.0
    downside = r[r < 0]
    if downside.empty:
        return 0.0
    dd_std = downside.std(ddof=1)
    if dd_std == 0 or pd.isna(dd_std):
        return 0.0
    return float(r.mean() / dd_std * math.sqrt(periods_per_year))


# ---------------------------------------------------------------- Drawdown


def compute_max_drawdown(equity_curve: pd.Series) -> float:
    """Max peak-to-trough drawdown of an equity curve, returned as a negative fraction.

    -0.25 means a 25% drawdown.
    """
    if equity_curve is None:
        return 0.0
    eq = pd.Series(equity_curve).dropna()
    if eq.empty:
        return 0.0
    peak = eq.cummax()
    dd = (eq - peak) / peak
    return float(dd.min()) if not dd.empty else 0.0


# ---------------------------------------------------------------- Calmar


def compute_calmar(returns: pd.Series, max_dd: float) -> float:
    """CAGR / |MaxDD|. If max_dd is 0, returns 0."""
    if returns is None or max_dd == 0:
        return 0.0
    r = pd.Series(returns).dropna()
    if r.empty:
        return 0.0
    cagr = float(r.mean()) * 252.0
    return cagr / abs(max_dd) if max_dd != 0 else 0.0


# ---------------------------------------------------------------- DSR


def expected_max_sharpe(n_trials: int) -> float:
    """E[max SR_i] across n_trials iid N(0,1) Sharpe candidates.

    Approximation (Bailey/LdP): sqrt(2 ln(N)) for N >= 2; 0 for N <= 1.
    """
    if n_trials <= 1:
        return 0.0
    return float(math.sqrt(2.0 * math.log(n_trials)))


def compute_deflated_sharpe(
    observed_sr: float,
    n_trials: int,
    n_samples: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """Deflated Sharpe Ratio per Bailey & López de Prado (2014).

    Parameters
    ----------
    observed_sr:
        Non-annualized SR (per period; same scale as `n_samples` count).
    n_trials:
        Number of independent strategy variants explored. Used to compute SR_0.
    n_samples:
        Number of observations the SR was estimated from.
    skew, kurt:
        Skewness and kurtosis of the per-period returns.
        `kurt=3.0` (normal) is the default — pass excess kurtosis + 3.

    Returns
    -------
    Probability under H0 that the true SR is greater than zero (so we want this
    close to 1.0, meaning the observation is hard to explain by luck).
    """
    if n_samples <= 1:
        return 0.0
    sr_0 = expected_max_sharpe(n_trials)
    denom = 1.0 - skew * observed_sr + ((kurt - 1.0) / 4.0) * observed_sr * observed_sr
    if denom <= 0:
        return 0.0
    num = (observed_sr - sr_0) * math.sqrt(max(0, n_samples - 1))
    z = num / math.sqrt(denom)
    return float(stats.norm.cdf(z))


# ---------------------------------------------------------------- PBO


def compute_pbo(
    in_sample_ranks: np.ndarray,
    out_sample_ranks: np.ndarray,
) -> float:
    """Probability of Backtest Overfitting via Combinatorially Symmetric CV.

    Inputs
    ------
    in_sample_ranks:
        2-D array, shape (n_splits, n_strategies). Each row is the rank
        (0..n_strategies-1, higher = better) of each strategy in that split's
        in-sample partition.
    out_sample_ranks:
        Same shape; out-of-sample ranks.

    Returns
    -------
    Fraction of splits where the best-IS strategy has a sub-median OOS rank.

    Notes
    -----
    Equivalent to Bailey et al. 2014's logit formulation; we compute the
    median-or-below probability directly since that's the parameter PBO
    estimates. logit transform is just rank-based and monotonic in this
    quantity.
    """
    is_ranks = np.asarray(in_sample_ranks, dtype=float)
    oos_ranks = np.asarray(out_sample_ranks, dtype=float)
    if is_ranks.shape != oos_ranks.shape:
        raise ValueError("in_sample_ranks and out_sample_ranks shapes differ")
    if is_ranks.ndim != 2:
        raise ValueError("ranks arrays must be 2-D")
    n_splits, n_strategies = is_ranks.shape
    if n_splits == 0 or n_strategies < 2:
        return 0.0

    median_rank = (n_strategies - 1) / 2.0
    # For each split pick the IS-best strategy and check whether its OOS rank
    # is at or below the median.
    best_is = np.argmax(is_ranks, axis=1)
    oos_at_best = oos_ranks[np.arange(n_splits), best_is]
    overfit_flag = (oos_at_best < median_rank).astype(float)
    # Ties contribute 0.5 (consistent with Bailey et al.).
    ties = np.isclose(oos_at_best, median_rank)
    overfit_flag[ties] = 0.5
    return float(overfit_flag.mean())


# ---------------------------------------------------------------- Ruin probability


def compute_ruin_probability(
    per_trade_mean_r: float,
    per_trade_std_r: float,
    dd_threshold_pct: float,
    n_trades: int,
    risk_per_trade_pct: float,
    n_paths: int = 5000,
    seed: int = 42,
) -> float:
    """Monte-Carlo ruin probability.

    Simulates n_paths paths of length n_trades. Each trade's return as a
    fraction of equity is `risk_per_trade_pct/100 * N(mean_r, std_r)`. Equity
    multiplies by (1 + r). Returns fraction of paths that touch the
    `dd_threshold_pct` (e.g. 20 -> 20% drawdown from running peak).
    """
    if n_trades <= 0 or risk_per_trade_pct <= 0 or dd_threshold_pct <= 0:
        return 0.0
    rng = np.random.default_rng(seed)
    risk_frac = risk_per_trade_pct / 100.0
    threshold_frac = dd_threshold_pct / 100.0
    # Pre-allocate.
    r_draws = rng.normal(per_trade_mean_r, per_trade_std_r, size=(int(n_paths), int(n_trades)))
    returns = 1.0 + r_draws * risk_frac
    equity = np.cumprod(returns, axis=1)
    # Track running peak per path.
    peaks = np.maximum.accumulate(equity, axis=1)
    drawdowns = (equity - peaks) / peaks
    worst = drawdowns.min(axis=1)
    return float((worst <= -threshold_frac).mean())


# ---------------------------------------------------------------- summary


def summarize(
    trades: pd.DataFrame,
    equity_curve: pd.Series,
    bar_seconds: int = 300,
) -> Dict[str, float]:
    """Bundle metrics commonly displayed for a backtest run.

    Sharpe is annualized using bar-frequency derived from `bar_seconds`. For
    sparse trade data we additionally surface trade-level summary stats
    (win-rate, profit factor, mean R).
    """
    metrics: Dict[str, float] = {}
    if equity_curve is None or equity_curve.empty:
        return metrics

    eq_returns = equity_curve.pct_change().dropna()
    periods_per_year = _safe_periods_per_year_from_bar_seconds(bar_seconds)
    metrics["sharpe"] = compute_sharpe(eq_returns, periods_per_year=periods_per_year)
    metrics["sortino"] = compute_sortino(eq_returns, periods_per_year=periods_per_year)
    metrics["max_drawdown"] = compute_max_drawdown(equity_curve)
    metrics["calmar"] = compute_calmar(eq_returns, metrics["max_drawdown"])
    metrics["total_return"] = float(equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0)

    if trades is None or trades.empty:
        metrics["n_trades"] = 0
        return metrics

    wins = trades.loc[trades["pnl"] > 0, "pnl"]
    losses = trades.loc[trades["pnl"] < 0, "pnl"]
    metrics["n_trades"] = int(len(trades))
    metrics["win_rate"] = float(len(wins) / len(trades)) if len(trades) else 0.0
    metrics["profit_factor"] = (
        float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 and not losses.empty else float("inf") if not wins.empty else 0.0
    )
    metrics["mean_r"] = float(trades["r_multiple"].mean()) if "r_multiple" in trades else 0.0
    metrics["std_r"] = float(trades["r_multiple"].std(ddof=1)) if "r_multiple" in trades and len(trades) > 1 else 0.0
    metrics["mean_hold_hours"] = (
        float(trades["hold_hours"].mean()) if "hold_hours" in trades else 0.0
    )
    return metrics
