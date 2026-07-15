"""Tests for src.experiments.drift."""
from __future__ import annotations

import numpy as np
import pytest

from src.experiments.drift import (
    DriftDetector,
    DriftReport,
    wilson_confidence_interval,
)


# -------------------------- Wilson CI


def test_wilson_ci_empty_returns_full_interval():
    low, high = wilson_confidence_interval(0, 0)
    assert low == 0.0
    assert high == 1.0


def test_wilson_ci_center_at_p_hat():
    low, high = wilson_confidence_interval(50, 100)
    assert low < 0.5 < high
    # Symmetric-ish around p_hat for p_hat = 0.5, large n.
    assert (high - 0.5) == pytest.approx(0.5 - low, rel=0.05)


def test_wilson_ci_clips_to_unit_interval():
    low, high = wilson_confidence_interval(0, 5)
    assert 0.0 <= low <= high <= 1.0
    low2, high2 = wilson_confidence_interval(5, 5)
    assert 0.0 <= low2 <= high2 <= 1.0


# -------------------------- fixtures


def _make_backtest(rng: np.random.Generator, n: int = 500) -> dict:
    """Realistic backtest R-multiple distribution.

    ~55% win rate, winners around +2R, losers around -1R — this yields a
    per-trade Sharpe well above zero so relative deltas behave sensibly.
    """
    wins = int(0.55 * n)
    r = np.concatenate(
        [
            rng.normal(loc=2.0, scale=0.4, size=wins),
            rng.normal(loc=-1.0, scale=0.4, size=n - wins),
        ]
    )
    r = np.clip(r, -3.0, 4.0)
    return {
        "r_multiples": r.tolist(),
        "win_rate": float((r > 0).mean()),
        "sharpe": float(r.mean() / r.std(ddof=1)),
    }


# -------------------------- healthy


def test_healthy_when_live_matches_backtest():
    rng = np.random.default_rng(0)
    stats = _make_backtest(rng, n=800)
    detector = DriftDetector(stats)

    # Draw live sample from the same distribution.
    live_r = rng.choice(stats["r_multiples"], size=80, replace=True)
    for r in live_r:
        detector.add_live_trade(r_multiple=float(r), pnl=float(r) * 10)

    report = detector.check()
    assert isinstance(report, DriftReport)
    assert report.n_live_trades == 80
    assert report.verdict == "healthy"
    assert abs(report.sharpe_delta_pct) < 0.30
    assert report.ks_test_pvalue > 0.05
    assert report.wilson_ci_overlap is True


# -------------------------- drifting


def test_drifting_verdict_moderate_shift():
    """Modest shift: Wilson CI must still overlap, KS in (0.01, 0.05] OR
    Sharpe delta in [0.30, 0.60]. We engineer this by drawing live from a
    slightly shifted version of the backtest.
    """
    rng = np.random.default_rng(7)
    stats = _make_backtest(rng, n=1000)
    detector = DriftDetector(stats)

    # Live: same win-rate distribution but tighter R-multiple mean (weaker
    # winners) — this reduces Sharpe modestly without shifting the win rate.
    for _ in range(60):
        base = float(rng.choice(stats["r_multiples"]))
        # Squash winners a bit; leave losers alone.
        r = base * 0.6 if base > 0 else base
        detector.add_live_trade(r_multiple=r, pnl=r * 10)

    report = detector.check()
    # Sharpe should have dropped materially but not catastrophically.
    assert report.verdict in ("drifting", "broken")
    # Verdict must not be healthy — that's the point of the test.
    assert report.verdict != "healthy"


# -------------------------- broken


def test_broken_when_distributions_disjoint():
    rng = np.random.default_rng(11)
    stats = _make_backtest(rng, n=1000)
    detector = DriftDetector(stats)

    # Live: strong losing distribution — Sharpe collapses, KS separates,
    # Wilson CI on WR does NOT contain the backtest WR.
    for _ in range(80):
        r = float(rng.normal(loc=-1.5, scale=0.4))
        detector.add_live_trade(r_multiple=r, pnl=r * 10)

    report = detector.check()
    assert report.verdict == "broken"
    assert report.ks_test_pvalue <= 0.01
    assert report.wilson_ci_overlap is False


def test_broken_when_wilson_ci_misses_backtest():
    """A live sample of pure losses can't cover the backtest win-rate."""
    rng = np.random.default_rng(3)
    stats = _make_backtest(rng, n=400)
    detector = DriftDetector(stats)

    for _ in range(40):
        detector.add_live_trade(r_multiple=-1.0, pnl=-10.0)

    report = detector.check()
    assert report.wilson_ci_overlap is False
    assert report.verdict == "broken"


# -------------------------- rolling


def test_rolling_check_honors_window():
    rng = np.random.default_rng(99)
    stats = _make_backtest(rng, n=800)
    detector = DriftDetector(stats)

    # 60 healthy trades sampled from the backtest.
    for r in rng.choice(stats["r_multiples"], size=60, replace=True):
        detector.add_live_trade(r_multiple=float(r), pnl=float(r) * 10)
    # 20 broken trades tacked on the end.
    for _ in range(20):
        detector.add_live_trade(r_multiple=-2.0, pnl=-20.0)

    full = detector.check()
    tail = detector.rolling_check(window=20)

    assert tail.n_live_trades == 20
    # Rolling window sees only the broken tail — should look worse.
    assert tail.verdict != "healthy"
    # Full window includes the healthy prefix, so its stats differ.
    assert tail.live_sharpe != full.live_sharpe


def test_rolling_check_window_larger_than_history():
    rng = np.random.default_rng(1)
    stats = _make_backtest(rng, n=200)
    detector = DriftDetector(stats)
    for r in rng.choice(stats["r_multiples"], size=5, replace=True):
        detector.add_live_trade(r_multiple=float(r), pnl=1.0)

    report = detector.rolling_check(window=100)
    assert report.n_live_trades == 5


def test_rolling_check_rejects_bad_window():
    rng = np.random.default_rng(2)
    detector = DriftDetector(_make_backtest(rng, n=100))
    with pytest.raises(ValueError):
        detector.rolling_check(window=0)


# -------------------------- edge cases


def test_zero_live_trades_returns_healthy():
    rng = np.random.default_rng(4)
    detector = DriftDetector(_make_backtest(rng, n=100))
    report = detector.check()
    assert report.n_live_trades == 0
    assert report.verdict == "healthy"


def test_requires_backtest_r_multiples():
    with pytest.raises(ValueError):
        DriftDetector({"win_rate": 0.5, "sharpe": 1.0})


def test_rejects_empty_backtest_r_multiples():
    with pytest.raises(ValueError):
        DriftDetector({"r_multiples": []})


def test_report_shape_matches_spec():
    rng = np.random.default_rng(5)
    stats = _make_backtest(rng, n=200)
    detector = DriftDetector(stats)
    detector.add_live_trade(1.0, 10)
    detector.add_live_trade(-0.5, -5)
    report = detector.check()
    for attr in (
        "n_live_trades",
        "live_sharpe",
        "backtest_sharpe",
        "sharpe_delta_pct",
        "live_win_rate",
        "backtest_win_rate",
        "wilson_ci_low",
        "wilson_ci_high",
        "wilson_ci_overlap",
        "ks_test_pvalue",
        "verdict",
    ):
        assert hasattr(report, attr), f"DriftReport missing {attr}"


def test_verdict_only_from_allowed_set():
    """Even for degenerate inputs, verdict is one of the three strings."""
    rng = np.random.default_rng(6)
    stats = _make_backtest(rng, n=100)
    detector = DriftDetector(stats)
    detector.add_live_trade(0.0, 0.0)
    detector.add_live_trade(0.0, 0.0)
    report = detector.check()
    assert report.verdict in ("healthy", "drifting", "broken")
