"""Backtest oracle for meta-loop validation.

A ProposedChange is applied to a SHADOW configuration (not live), the
backtest harness replays held-out historical data with the shadow config,
and the oracle returns a structured result. The change deploys only if
the result passes the gates below.

Wired against the Phase 0 BacktestRunner (src.backtest.runner). The
oracle deliberately does NOT own the harness, base config, or held-out
data — the caller assembles them and passes them in so this file has no
knowledge of live trading state (Tier D firewall: the oracle must not
be mutable by the meta-loop it judges).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from src.backtest.metrics import compute_ruin_probability
from src.backtest.runner import BacktestRunner
from src.meta.proposer import ProposedChange
from src.meta.regime_archives import (
    RegimeArchive,
    RegimeArchiveLoader,
    RegimeArchiveMissing,
)
from src.meta.shadow_config import ShadowConfig
from src.signals.llm_signal_generator import (
    CONFIDENCE_RECONCILE_TOLERANCE,
    MAX_CONFIDENCE,
    MIN_KILL_THESIS_LEN,
    MIN_NONZERO_LENSES,
    MIN_RR_MULTIPLE,
)


@dataclass
class OracleResult:
    change_id: str
    passed: bool
    reason: str                       # one-line summary of pass/fail
    metrics: Dict[str, float]         # sharpe, win_rate, avg_r, max_dd, dsr, pbo, ruin_prob
    baseline_metrics: Dict[str, float]
    regimes_tested: List[str]         # e.g. ["trending_bull_2024Q1", "sideways_2024Q3"]
    regimes_failed: List[str]


# Gate constants — keep in sync with plan/meta_learning_loop.md §IV
SHARPE_IMPROVEMENT_MIN = 0.10
PBO_MAX = 0.20
DSR_MIN = 0.95


# Tier-C anti-loosening firewall table.
# direction == "lower_is_looser": a smaller after-value would weaken validate_take.
# direction == "higher_is_looser": a larger after-value would weaken validate_take.
_TIER_C_GUARDS = {
    "MIN_KILL_THESIS_LEN": (MIN_KILL_THESIS_LEN, "lower_is_looser"),
    "MIN_RR_MULTIPLE": (MIN_RR_MULTIPLE, "lower_is_looser"),
    "MIN_NONZERO_LENSES": (MIN_NONZERO_LENSES, "lower_is_looser"),
    "MAX_CONFIDENCE": (MAX_CONFIDENCE, "higher_is_looser"),
    "CONFIDENCE_RECONCILE_TOLERANCE": (CONFIDENCE_RECONCILE_TOLERANCE, "higher_is_looser"),
}


def _tier_c_loosening_check(change: ProposedChange) -> None:
    """Reject any change that would loosen a Tier-C validate_take threshold.

    Recognised diff shapes (see plan/dual_purpose_architecture.md anti-runaway
    firewall #1):

        {"constant": "MIN_RR_MULTIPLE", "before": 3.0, "after": 2.5, ...}
        {"path": [..., "MIN_RR_MULTIPLE"], "before": 3.0, "after": 2.5, ...}

    Anything else that touches a Tier-C constant name is treated as suspect
    and rejected — Tier-C thresholds are manual-only per the approval matrix.
    """
    diff = change.diff or {}

    # Collect (constant_name, proposed_after_value) references from the diff.
    references: List[tuple] = []
    if isinstance(diff.get("constant"), str):
        references.append((diff["constant"], diff.get("after")))
    path = diff.get("path")
    if isinstance(path, list) and path:
        last = path[-1]
        if isinstance(last, str):
            references.append((last, diff.get("after")))
    # Nested-list form used by multi-target proposals.
    if isinstance(diff.get("changes"), list):
        for sub in diff["changes"]:
            if not isinstance(sub, dict):
                continue
            if isinstance(sub.get("constant"), str):
                references.append((sub["constant"], sub.get("after")))
            sub_path = sub.get("path")
            if isinstance(sub_path, list) and sub_path and isinstance(sub_path[-1], str):
                references.append((sub_path[-1], sub.get("after")))

    for name, after_val in references:
        if name not in _TIER_C_GUARDS:
            continue
        current, direction = _TIER_C_GUARDS[name]
        # Any TIER-C reference without a numeric after → reject conservatively.
        if not isinstance(after_val, (int, float)):
            raise ValueError(
                f"Tier-C constant {name} referenced in diff without a numeric "
                f"'after' value; Tier-C thresholds are manual-only"
            )
        if direction == "lower_is_looser" and after_val < current:
            raise ValueError(
                f"Tier-C loosening rejected: {name} would drop from {current} "
                f"to {after_val} (validate_take firewall)"
            )
        if direction == "higher_is_looser" and after_val > current:
            raise ValueError(
                f"Tier-C loosening rejected: {name} would rise from {current} "
                f"to {after_val} (validate_take firewall)"
            )


def _estimate_ruin_prob(verdict: Dict[str, Any]) -> float:
    """Approximate ruin probability from the runner's in-sample summary.

    The runner exposes {sharpe, trades, win_rate, total_return, max_drawdown}
    but not per-trade R stats. We approximate a symmetric ±1R return per
    trade (mean_r = 2*win_rate - 1, std_r = 1) and Monte-Carlo the drawdown
    to a 20% floor with 1% risk per trade. It's a coarse proxy — good
    enough to detect a shadow that is dramatically more fragile than
    baseline (which is all the gate needs).
    """
    is_ = verdict.get("in_sample", {}) or {}
    n_trades = int(is_.get("trades", 0) or 0)
    if n_trades == 0:
        # No trades → oracle cannot say the config is safer; treat as max risk.
        return 1.0
    win_rate = float(is_.get("win_rate", 0.0) or 0.0)
    mean_r = 2.0 * win_rate - 1.0
    return float(
        compute_ruin_probability(
            per_trade_mean_r=mean_r,
            per_trade_std_r=1.0,
            dd_threshold_pct=20.0,
            n_trades=max(n_trades, 100),
            risk_per_trade_pct=1.0,
            n_paths=500,
            seed=17,
        )
    )


def _extract_metrics(verdict: Dict[str, Any]) -> Dict[str, float]:
    """Flatten runner's verdict into the OracleResult metrics dict."""
    is_ = verdict.get("in_sample", {}) or {}
    return {
        "sharpe": float(is_.get("sharpe", 0.0) or 0.0),
        "win_rate": float(is_.get("win_rate", 0.0) or 0.0),
        "avg_r": 0.0,  # runner doesn't surface this; add when it does
        "max_dd": float(is_.get("max_drawdown", 0.0) or 0.0),
        "trades": float(is_.get("trades", 0) or 0),
        "total_return": float(is_.get("total_return", 0.0) or 0.0),
        "dsr": float(verdict.get("dsr", 0.0) or 0.0),
        "pbo": float(verdict.get("pbo", 0.0) or 0.0),
        "ruin_prob": _estimate_ruin_prob(verdict),
    }


class BacktestOracle:
    """Validates a ProposedChange against held-out historical data.

    The oracle must be DETERMINISTIC and not modifiable by the meta-loop
    itself (Tier D forbidden — see plan §III).

    Constructor
    -----------
    backtest_harness:
        `BacktestRunner` instance owned by the caller. The oracle does not
        construct it, so the caller controls the strategy_factory, cost
        model, walk-forward geometry, and CPCV params.
    base_config:
        The current live config dict. Diffs are applied on a deep-copy of
        this (never mutated in place). Optional so tests can instantiate
        with just the harness — pass a config when calling `validate`.
    held_out_candles:
        The primary holdout window. Same schema as EventDrivenSimulator
        (open/high/low/close/volume, monotonic-increasing index).
    feature_pack_provider:
        Called for each bar during evaluation. For the phase-0 harness
        an empty-dict provider works; production wires a real one.
    regime_loader:
        Optional. When absent, regime testing is skipped (regimes_tested
        will be empty). In production the loader is always supplied.
    """

    def __init__(
        self,
        backtest_harness: BacktestRunner,
        base_config: Optional[Dict[str, Any]] = None,
        held_out_candles: Optional[pd.DataFrame] = None,
        feature_pack_provider: Optional[Callable[[pd.Timestamp], Dict[str, Any]]] = None,
        regime_loader: Optional[RegimeArchiveLoader] = None,
    ) -> None:
        self.harness = backtest_harness
        self.base_config = base_config or {}
        self.held_out_candles = held_out_candles
        self.feature_pack_provider = feature_pack_provider or (lambda _ts: {})
        self.regime_loader = regime_loader

    # ------------------------------------------------------------ public

    def validate(self, change: ProposedChange) -> OracleResult:
        """Apply change to a shadow config, run backtest, return result.

        Pass criteria (all required):
          - Sharpe improves by >= SHARPE_IMPROVEMENT_MIN vs baseline
          - PBO <= PBO_MAX
          - DSR >= DSR_MIN
          - Ruin probability does not increase vs baseline
          - Passes in ALL regime archives tested (no per-regime opt-outs)

        Raises
        ------
        ValueError
            If `change` attempts to loosen any Tier-C threshold.
        RuntimeError
            If the oracle is missing a held-out window at call time.
        """
        # 1. Tier-C firewall — RAISES, does not return a failed OracleResult.
        _tier_c_loosening_check(change)

        # 2. Build the shadow config.
        shadow_builder = ShadowConfig(self.base_config)
        shadow_cfg = shadow_builder.apply(change)

        # 3. Baseline + shadow on the primary holdout window.
        if self.held_out_candles is None or len(self.held_out_candles) == 0:
            raise RuntimeError(
                "BacktestOracle.validate: held_out_candles is empty; "
                "the oracle needs a holdout window to compare shadow vs baseline"
            )

        baseline_verdict = self._run(
            hypothesis_id=f"baseline-{change.change_id}",
            config=self.base_config,
            candles=self.held_out_candles,
        )
        shadow_verdict = self._run(
            hypothesis_id=f"shadow-{change.change_id}",
            config=shadow_cfg,
            candles=self.held_out_candles,
        )

        baseline_metrics = _extract_metrics(baseline_verdict)
        shadow_metrics = _extract_metrics(shadow_verdict)

        # 4. Regime testing — per-regime degradation guard.
        regimes_tested, regimes_failed, regime_reasons = self._run_regime_tests(
            change=change,
            shadow_cfg=shadow_cfg,
        )

        # 5. Assemble fail reasons.
        fail_reasons: List[str] = []
        sharpe_delta = shadow_metrics["sharpe"] - baseline_metrics["sharpe"]
        if sharpe_delta < SHARPE_IMPROVEMENT_MIN:
            fail_reasons.append(
                f"Sharpe delta {sharpe_delta:.3f} < {SHARPE_IMPROVEMENT_MIN}"
            )
        if shadow_metrics["pbo"] > PBO_MAX:
            fail_reasons.append(
                f"PBO {shadow_metrics['pbo']:.3f} > {PBO_MAX}"
            )
        if shadow_metrics["dsr"] < DSR_MIN:
            fail_reasons.append(
                f"DSR {shadow_metrics['dsr']:.3f} < {DSR_MIN}"
            )
        if shadow_metrics["ruin_prob"] > baseline_metrics["ruin_prob"]:
            fail_reasons.append(
                f"ruin_prob {shadow_metrics['ruin_prob']:.3f} > "
                f"baseline {baseline_metrics['ruin_prob']:.3f}"
            )
        if regimes_failed:
            fail_reasons.append(
                "regime failures: " + "; ".join(regime_reasons)
            )

        passed = not fail_reasons
        reason = "passed all gates" if passed else " | ".join(fail_reasons)

        return OracleResult(
            change_id=change.change_id,
            passed=passed,
            reason=reason,
            metrics=shadow_metrics,
            baseline_metrics=baseline_metrics,
            regimes_tested=regimes_tested,
            regimes_failed=regimes_failed,
        )

    # ------------------------------------------------------------ helpers

    def _run(
        self,
        hypothesis_id: str,
        config: Dict[str, Any],
        candles: pd.DataFrame,
    ) -> Dict[str, Any]:
        """Feed one config → hypothesis → BacktestRunner. Returns the runner's verdict dict."""
        hypothesis = dict(config)
        hypothesis["id"] = hypothesis_id
        return self.harness.evaluate_hypothesis(
            hypothesis_yaml=hypothesis,
            candles=candles,
            feature_pack_provider=self.feature_pack_provider,
        )

    def _run_regime_tests(
        self,
        change: ProposedChange,
        shadow_cfg: Dict[str, Any],
    ) -> tuple:
        """Run baseline + shadow across each regime archive.

        Returns (names_tested, names_failed, per_regime_reason_strings).

        Fail criterion (per plan §IV.4 + task spec): if the shadow
        DEGRADES in ANY regime by more than SHARPE_IMPROVEMENT_MIN
        (i.e. baseline_sharpe - shadow_sharpe > SHARPE_IMPROVEMENT_MIN),
        that regime is marked failed. There are no per-regime opt-outs.
        """
        names_tested: List[str] = []
        names_failed: List[str] = []
        reasons: List[str] = []

        if self.regime_loader is None:
            return names_tested, names_failed, reasons

        try:
            regimes: List[RegimeArchive] = self.regime_loader.load_all()
        except RegimeArchiveMissing as e:
            # Missing archives are a fatal setup error — surface, don't paper over.
            raise RuntimeError(
                f"BacktestOracle.validate: {e}. Run "
                f"build_synthetic_regime_archives() to materialize fixtures."
            ) from e

        for regime in regimes:
            names_tested.append(regime.name)
            if regime.candles.empty:
                names_failed.append(regime.name)
                reasons.append(f"{regime.name}: empty archive")
                continue

            baseline_v = self._run(
                hypothesis_id=f"baseline-{change.change_id}-{regime.name}",
                config=self.base_config,
                candles=regime.candles,
            )
            shadow_v = self._run(
                hypothesis_id=f"shadow-{change.change_id}-{regime.name}",
                config=shadow_cfg,
                candles=regime.candles,
            )
            b_sh = float(baseline_v.get("in_sample", {}).get("sharpe", 0.0) or 0.0)
            s_sh = float(shadow_v.get("in_sample", {}).get("sharpe", 0.0) or 0.0)
            degradation = b_sh - s_sh
            if degradation > SHARPE_IMPROVEMENT_MIN:
                names_failed.append(regime.name)
                reasons.append(
                    f"{regime.name}: shadow Sharpe {s_sh:.3f} vs baseline "
                    f"{b_sh:.3f} (degrades by {degradation:.3f} > "
                    f"{SHARPE_IMPROVEMENT_MIN})"
                )

        return names_tested, names_failed, reasons
