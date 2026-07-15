"""Live-vs-backtest drift detector.

Once the strategy is running paper (Phase 1) or live (Phase 2), we need
early warning when the realised distribution stops matching the reference
backtest. This module supplies:

* Wilson 95% confidence interval on the live win-rate (does it still cover
  the backtest win-rate?)
* KS 2-sample test on the R-multiple distribution (are the two samples
  plausibly from the same distribution?)
* Sharpe delta as fraction of backtest Sharpe

The three signals are combined into a ``healthy | drifting | broken`` verdict
per the operational-policies gate (see ``plan/operational_policies.md``, the
"Variance vs signal" section).

Verdict thresholds (per Track F spec):

* healthy  : |sharpe delta pct| < 0.30 AND KS p > 0.05 AND Wilson CI overlaps
* drifting : |sharpe delta pct| in [0.30, 0.60] OR KS p in (0.01, 0.05]
* broken   : |sharpe delta pct| > 0.60 OR KS p <= 0.01 OR Wilson CI DOES NOT overlap
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from scipy import stats


WILSON_Z_95 = 1.959963984540054  # scipy.stats.norm.ppf(0.975)


Verdict = Literal["healthy", "drifting", "broken"]


# ---------------------------------------------------------------- Wilson CI


def wilson_confidence_interval(
    successes: int, n: int, z: float = WILSON_Z_95
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Handles ``n == 0`` by returning ``(0.0, 1.0)`` — no information yet.
    """
    if n <= 0:
        return 0.0, 1.0
    p_hat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    margin = (
        z
        * math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n)
        / denom
    )
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return low, high


def _per_trade_sharpe(r_multiples) -> float:
    r = np.asarray(list(r_multiples), dtype=float)
    if r.size < 2:
        return 0.0
    std = float(r.std(ddof=1))
    if std == 0 or math.isnan(std):
        return 0.0
    return float(r.mean() / std)


def _pct_delta(live: float, bt: float) -> float:
    """Signed relative delta ``(live - bt) / bt`` with graceful fallbacks."""
    if bt == 0 or math.isnan(bt) or math.isinf(bt):
        # No meaningful baseline; treat any live value as an absolute delta.
        return float(live)
    return float((live - bt) / bt)


# ---------------------------------------------------------------- DriftReport


@dataclass
class DriftReport:
    n_live_trades: int
    live_sharpe: float
    backtest_sharpe: float
    sharpe_delta_pct: float  # (live - bt) / bt
    live_win_rate: float
    backtest_win_rate: float
    wilson_ci_low: float
    wilson_ci_high: float
    wilson_ci_overlap: bool
    ks_test_pvalue: float
    verdict: Verdict


# ---------------------------------------------------------------- DriftDetector


@dataclass
class _LiveTrade:
    r_multiple: float
    pnl: float


class DriftDetector:
    """Compare a rolling stream of live trades to a reference backtest.

    Parameters
    ----------
    backtest_stats:
        Dict with keys ``sharpe`` (float), ``win_rate`` (float in [0, 1]),
        and ``r_multiples`` (iterable of floats). Only ``r_multiples`` is
        strictly required — Sharpe / win rate are derived from it when
        absent, so the caller can supply either the raw sample or the
        pre-computed summary.
    """

    def __init__(self, backtest_stats: dict) -> None:
        raw_r = backtest_stats.get("r_multiples")
        if raw_r is None:
            raise ValueError("backtest_stats['r_multiples'] is required")
        self._backtest_r = np.asarray(list(raw_r), dtype=float)
        if self._backtest_r.size == 0:
            raise ValueError("backtest_stats['r_multiples'] must be non-empty")

        provided_sharpe = backtest_stats.get("sharpe")
        self._backtest_sharpe = (
            float(provided_sharpe)
            if provided_sharpe is not None
            else _per_trade_sharpe(self._backtest_r)
        )

        provided_wr = backtest_stats.get("win_rate")
        self._backtest_wr = (
            float(provided_wr)
            if provided_wr is not None
            else float((self._backtest_r > 0).mean())
        )

        self._live: list[_LiveTrade] = []

    # -------------------------- ingestion

    def add_live_trade(self, r_multiple: float, pnl: float) -> None:
        self._live.append(_LiveTrade(float(r_multiple), float(pnl)))

    def n_live(self) -> int:
        return len(self._live)

    def reset(self) -> None:
        self._live.clear()

    # -------------------------- checks

    def check(self) -> DriftReport:
        return self._compute(self._live)

    def rolling_check(self, window: int = 30) -> DriftReport:
        if window <= 0:
            raise ValueError("window must be > 0")
        subset = self._live[-int(window):]
        return self._compute(subset)

    # -------------------------- internal

    def _compute(self, trades: list[_LiveTrade]) -> DriftReport:
        n_live = len(trades)
        live_r = np.asarray([t.r_multiple for t in trades], dtype=float)

        # Sharpe
        live_sharpe = _per_trade_sharpe(live_r) if n_live else 0.0
        sharpe_delta_pct = _pct_delta(live_sharpe, self._backtest_sharpe)

        # Win rate + Wilson CI on the live sample
        wins = int((live_r > 0).sum()) if n_live else 0
        live_wr = wins / n_live if n_live else 0.0
        ci_low, ci_high = wilson_confidence_interval(wins, n_live)
        wilson_overlap = ci_low <= self._backtest_wr <= ci_high

        # KS test on distributions of R-multiples
        ks_p = self._ks_pvalue(live_r)

        verdict = self._classify(
            sharpe_delta_pct=sharpe_delta_pct,
            ks_pvalue=ks_p,
            wilson_overlap=wilson_overlap,
            n_live=n_live,
        )

        return DriftReport(
            n_live_trades=n_live,
            live_sharpe=float(live_sharpe),
            backtest_sharpe=float(self._backtest_sharpe),
            sharpe_delta_pct=float(sharpe_delta_pct),
            live_win_rate=float(live_wr),
            backtest_win_rate=float(self._backtest_wr),
            wilson_ci_low=float(ci_low),
            wilson_ci_high=float(ci_high),
            wilson_ci_overlap=bool(wilson_overlap),
            ks_test_pvalue=float(ks_p),
            verdict=verdict,
        )

    def _ks_pvalue(self, live_r: np.ndarray) -> float:
        if live_r.size < 2 or self._backtest_r.size < 2:
            # Not enough data to distinguish distributions — treat as no signal.
            return 1.0
        result = stats.ks_2samp(live_r, self._backtest_r)
        p = float(result.pvalue)
        if math.isnan(p):
            return 1.0
        return p

    @staticmethod
    def _classify(
        *,
        sharpe_delta_pct: float,
        ks_pvalue: float,
        wilson_overlap: bool,
        n_live: int,
    ) -> Verdict:
        """Apply the healthy / drifting / broken thresholds.

        Ordering matters: broken > drifting > healthy. When n_live is zero
        we can't measure anything — return healthy (no signal).
        """
        if n_live == 0:
            return "healthy"

        abs_delta = abs(sharpe_delta_pct)

        # Broken — any single trigger.
        if abs_delta > 0.60 or ks_pvalue <= 0.01 or not wilson_overlap:
            return "broken"

        # Drifting — either sharpe delta or moderate KS p-value.
        if 0.30 <= abs_delta <= 0.60 or (0.01 < ks_pvalue <= 0.05):
            return "drifting"

        # Healthy — all bands clear.
        return "healthy"
