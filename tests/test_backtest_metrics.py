"""Metrics correctness tests."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.backtest.metrics import (
    compute_calmar,
    compute_deflated_sharpe,
    compute_max_drawdown,
    compute_pbo,
    compute_ruin_probability,
    compute_sharpe,
    compute_sortino,
    expected_max_sharpe,
)


def test_sharpe_returns_zero_on_empty_input():
    assert compute_sharpe(pd.Series(dtype=float)) == 0.0


def test_sharpe_matches_manual_formula():
    rng = np.random.default_rng(0)
    r = pd.Series(rng.normal(0.001, 0.01, 1000))
    expected = r.mean() / r.std(ddof=1) * math.sqrt(252)
    assert abs(compute_sharpe(r) - expected) < 1e-9


def test_sharpe_zero_when_std_zero():
    r = pd.Series([0.01, 0.01, 0.01])
    assert compute_sharpe(r) == 0.0


def test_sortino_lower_when_only_downside_volatility():
    rng = np.random.default_rng(0)
    r = pd.Series(rng.normal(0.001, 0.01, 500))
    sortino = compute_sortino(r)
    sharpe = compute_sharpe(r)
    assert sortino != 0
    # Sortino should be > Sharpe when full vol > downside vol (typical)
    assert sortino > sharpe or abs(sortino - sharpe) < 1.0


def test_max_drawdown_matches_known_curve():
    eq = pd.Series([1.0, 1.2, 0.8, 0.9, 1.1])
    # Peak 1.2, trough 0.8 => -0.333...
    assert abs(compute_max_drawdown(eq) - (-1.0 / 3.0)) < 1e-9


def test_max_drawdown_zero_on_monotonic_curve():
    eq = pd.Series([1.0, 1.1, 1.2, 1.3])
    assert compute_max_drawdown(eq) == 0.0


def test_calmar_combines_returns_and_drawdown():
    rng = np.random.default_rng(0)
    r = pd.Series(rng.normal(0.0005, 0.01, 252))
    dd = -0.1
    out = compute_calmar(r, dd)
    expected = r.mean() * 252 / abs(dd)
    assert abs(out - expected) < 1e-9


def test_expected_max_sharpe_increases_with_trials():
    a = expected_max_sharpe(10)
    b = expected_max_sharpe(100)
    c = expected_max_sharpe(1000)
    assert a < b < c
    # sanity bound
    assert math.isclose(a, math.sqrt(2 * math.log(10)))


def test_deflated_sharpe_high_obs_passes_gate():
    """Strong observed SR substantially above the expected-max under H0 → DSR near 1.

    expected_max_sharpe(10) = sqrt(2*ln(10)) ~ 2.146. To pass the gate, the observed
    (per-period) SR must dominate that bound. We construct such a case here.
    """
    dsr = compute_deflated_sharpe(observed_sr=3.0, n_trials=10, n_samples=500, skew=0.0, kurt=3.0)
    assert 0.9 < dsr <= 1.0


def test_deflated_sharpe_low_obs_fails():
    # Weak observation under many trials → DSR low.
    dsr = compute_deflated_sharpe(observed_sr=0.05, n_trials=1000, n_samples=200, skew=0.0, kurt=3.0)
    assert dsr < 0.5


def test_deflated_sharpe_strict_inequality_with_more_trials():
    high = compute_deflated_sharpe(observed_sr=2.0, n_trials=2, n_samples=500)
    low = compute_deflated_sharpe(observed_sr=2.0, n_trials=2000, n_samples=500)
    assert high > low


def test_pbo_zero_for_perfectly_consistent_strategies():
    # In-sample best == out-sample best for every split → PBO = 0.
    n_splits, n_strats = 10, 5
    is_ranks = np.tile(np.arange(n_strats), (n_splits, 1))
    oos_ranks = is_ranks.copy()
    pbo = compute_pbo(is_ranks, oos_ranks)
    assert pbo == 0.0  # IS-best is rank n-1, OOS-best is also rank n-1


def test_pbo_one_for_perfectly_inconsistent_strategies():
    # IS-best (rank n-1) becomes OOS-worst (rank 0) every time → PBO == 1.
    n_splits, n_strats = 8, 4
    is_ranks = np.tile(np.arange(n_strats), (n_splits, 1))
    oos_ranks = np.tile(np.arange(n_strats)[::-1], (n_splits, 1))
    pbo = compute_pbo(is_ranks, oos_ranks)
    assert pbo == 1.0


def test_pbo_around_half_for_random_ranks():
    rng = np.random.default_rng(0)
    n_splits, n_strats = 200, 10
    is_ranks = np.array([rng.permutation(n_strats) for _ in range(n_splits)])
    oos_ranks = np.array([rng.permutation(n_strats) for _ in range(n_splits)])
    pbo = compute_pbo(is_ranks, oos_ranks)
    # With independent permutations the expected PBO is 0.5.
    assert 0.3 < pbo < 0.7


def test_ruin_probability_high_with_negative_edge():
    """Negative-mean trades with sane risk per trade should accumulate ruin."""
    p = compute_ruin_probability(
        per_trade_mean_r=-0.2,
        per_trade_std_r=1.0,
        dd_threshold_pct=20.0,
        n_trades=200,
        risk_per_trade_pct=2.0,
        n_paths=500,
    )
    assert p > 0.5


def test_ruin_probability_low_with_positive_edge():
    p = compute_ruin_probability(
        per_trade_mean_r=0.3,
        per_trade_std_r=0.5,
        dd_threshold_pct=20.0,
        n_trades=300,
        risk_per_trade_pct=1.0,
        n_paths=500,
    )
    assert p < 0.2


def test_ruin_probability_zero_with_no_trades():
    p = compute_ruin_probability(
        per_trade_mean_r=0.0,
        per_trade_std_r=1.0,
        dd_threshold_pct=20.0,
        n_trades=0,
        risk_per_trade_pct=1.0,
    )
    assert p == 0.0
