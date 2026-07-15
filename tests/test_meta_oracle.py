"""Tests for BacktestOracle.validate.

We swap the real BacktestRunner for a FakeHarness that returns pre-programmed
verdicts, so the tests exercise the oracle's gate logic deterministically
without depending on strategy semantics. Real-harness integration is
covered by test_backtest_runner.py already.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from src.meta.oracle import (
    DSR_MIN,
    PBO_MAX,
    SHARPE_IMPROVEMENT_MIN,
    BacktestOracle,
    OracleResult,
)
from src.meta.proposer import ProposedChange
from src.meta.regime_archives import (
    RegimeArchiveLoader,
    build_synthetic_regime_archives,
)


# ------------------------------------------------------------------ helpers


def _tiny_candles(n: int = 200) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    rng = np.random.default_rng(1)
    closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.001, n))
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.001,
            "low": np.minimum(opens, closes) * 0.999,
            "close": closes,
            "volume": np.full(n, 50.0),
        },
        index=idx,
    )


def _make_change(target="config_threshold", diff=None, change_id="chg-001", tier="A"):
    if diff is None:
        diff = {"path": ["GATE", "cooldown_seconds"], "before": 300, "after": 900}
    return ProposedChange(
        change_id=change_id,
        tier=tier,
        target=target,
        rationale="test",
        evidence=[],
        diff=diff,
        proposed_at=0,
        proposer_model="test",
        proposer_prompt_hash="deadbeef",
    )


class FakeHarness:
    """Programmable stand-in for BacktestRunner.

    `verdicts` is a mapping from a matcher-substring (matched against the
    hypothesis_id argument) to the verdict dict the harness should return.
    Longest-substring match wins so a specific `shadow-` matcher overrides
    a general `baseline-` matcher.
    """

    def __init__(self, verdicts: Dict[str, Dict[str, Any]]):
        self.verdicts = verdicts
        self.calls: List[str] = []

    def evaluate_hypothesis(
        self,
        hypothesis_yaml: Dict[str, Any],
        candles: pd.DataFrame,
        feature_pack_provider,
    ) -> Dict[str, Any]:
        hid = str(hypothesis_yaml.get("id", ""))
        self.calls.append(hid)
        # Longest matching key wins.
        best_key = ""
        for k in self.verdicts:
            if k in hid and len(k) > len(best_key):
                best_key = k
        if not best_key:
            raise KeyError(f"FakeHarness: no verdict matcher for '{hid}'")
        # Return a fresh copy so callers can't mutate our fixtures.
        v = self.verdicts[best_key]
        return {
            "hypothesis_id": hid,
            "in_sample": dict(v.get("in_sample", {})),
            "pbo": v.get("pbo", 0.0),
            "dsr": v.get("dsr", 1.0),
            "aggregated_oos_trades": v.get("aggregated_oos_trades", 500),
            "verdict": v.get("verdict", "PASS"),
            "fail_reasons": v.get("fail_reasons", []),
            "walk_forward": v.get("walk_forward", {}),
            "cpcv": v.get("cpcv", {}),
        }


def _v(sharpe, win_rate=0.6, trades=500, max_dd=-0.10, pbo=0.10, dsr=0.98):
    return {
        "in_sample": {
            "sharpe": sharpe,
            "trades": trades,
            "win_rate": win_rate,
            "total_return": 0.20,
            "max_drawdown": max_dd,
        },
        "pbo": pbo,
        "dsr": dsr,
    }


@pytest.fixture(scope="module")
def regime_loader(tmp_path_factory) -> RegimeArchiveLoader:
    d = tmp_path_factory.mktemp("regimes")
    build_synthetic_regime_archives(root=d, overwrite=True)
    return RegimeArchiveLoader(root=d)


@pytest.fixture
def base_config():
    return {"GATE": {"cooldown_seconds": 300}, "SIGNAL": {"min_signal_interval_seconds": 1800}}


# ------------------------------------------------------------- happy path


def test_validate_passes_when_all_gates_pass(regime_loader, base_config):
    """Shadow beats baseline everywhere; no gate fires."""
    harness = FakeHarness(
        {
            "baseline-": _v(sharpe=0.50, win_rate=0.55, dsr=0.97, pbo=0.10),
            "shadow-": _v(sharpe=0.80, win_rate=0.65, dsr=0.98, pbo=0.10),
        }
    )
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    result = oracle.validate(_make_change())
    assert isinstance(result, OracleResult)
    assert result.passed, f"expected pass, got: {result.reason}"
    assert result.reason == "passed all gates"
    assert set(result.regimes_tested) == {"trending_bull", "trending_bear", "sideways"}
    assert result.regimes_failed == []
    assert result.metrics["sharpe"] > result.baseline_metrics["sharpe"]


# ------------------------------------------------------------- gate failures


def test_validate_fails_on_sharpe_gate(regime_loader, base_config):
    """Shadow Sharpe barely inches ahead — below SHARPE_IMPROVEMENT_MIN."""
    harness = FakeHarness(
        {
            "baseline-": _v(sharpe=0.50),
            "shadow-": _v(sharpe=0.55),  # +0.05 < 0.10
        }
    )
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    result = oracle.validate(_make_change())
    assert not result.passed
    assert "Sharpe delta" in result.reason


def test_validate_fails_on_pbo_gate(regime_loader, base_config):
    harness = FakeHarness(
        {
            "baseline-": _v(sharpe=0.50, pbo=0.10),
            "shadow-": _v(sharpe=0.80, pbo=0.35),  # > 0.20
        }
    )
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    result = oracle.validate(_make_change())
    assert not result.passed
    assert "PBO" in result.reason


def test_validate_fails_on_dsr_gate(regime_loader, base_config):
    harness = FakeHarness(
        {
            "baseline-": _v(sharpe=0.50, dsr=0.97),
            "shadow-": _v(sharpe=0.80, dsr=0.90),  # < 0.95
        }
    )
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    result = oracle.validate(_make_change())
    assert not result.passed
    assert "DSR" in result.reason


def test_validate_fails_when_ruin_prob_increases(regime_loader, base_config):
    """Shadow has lower win-rate → higher estimated ruin_prob than baseline.

    Sharpe improvement satisfied so this specifically isolates the ruin gate.
    """
    harness = FakeHarness(
        {
            "baseline-": _v(sharpe=0.50, win_rate=0.60),   # mean_r = 0.20
            "shadow-": _v(sharpe=0.80, win_rate=0.45),     # mean_r = -0.10 (much riskier)
        }
    )
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    result = oracle.validate(_make_change())
    assert not result.passed
    assert "ruin_prob" in result.reason


def test_validate_fails_when_regime_degrades(base_config, tmp_path):
    """Bull regime intentionally degraded → whole change blocked."""
    build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    loader = RegimeArchiveLoader(root=tmp_path)
    # Primary window (identified by "chg-001" only, no regime suffix) passes.
    # Trending_bull explicitly loses > SHARPE_IMPROVEMENT_MIN.
    harness = FakeHarness(
        {
            "baseline-chg-001": _v(sharpe=0.50),
            "shadow-chg-001": _v(sharpe=0.80),
            "baseline-chg-001-trending_bull": _v(sharpe=0.90),
            "shadow-chg-001-trending_bull": _v(sharpe=0.30),  # -0.60 vs baseline
            "baseline-chg-001-trending_bear": _v(sharpe=0.50),
            "shadow-chg-001-trending_bear": _v(sharpe=0.55),
            "baseline-chg-001-sideways": _v(sharpe=0.20),
            "shadow-chg-001-sideways": _v(sharpe=0.25),
        }
    )
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=loader,
    )
    result = oracle.validate(_make_change())
    assert not result.passed
    assert "trending_bull" in result.regimes_failed
    assert "trending_bull" in result.reason
    # The non-failing regimes are still recorded as tested.
    assert "trending_bear" in result.regimes_tested
    assert "sideways" in result.regimes_tested


def test_validate_passes_when_regimes_only_slightly_degrade(base_config, tmp_path):
    """Small per-regime dip < SHARPE_IMPROVEMENT_MIN is tolerable."""
    build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    loader = RegimeArchiveLoader(root=tmp_path)
    harness = FakeHarness(
        {
            "baseline-chg-001": _v(sharpe=0.50),
            "shadow-chg-001": _v(sharpe=0.80),
            "baseline-chg-001-trending_bull": _v(sharpe=0.90),
            "shadow-chg-001-trending_bull": _v(sharpe=0.85),  # -0.05 tolerable
            "baseline-chg-001-trending_bear": _v(sharpe=0.50),
            "shadow-chg-001-trending_bear": _v(sharpe=0.55),
            "baseline-chg-001-sideways": _v(sharpe=0.20),
            "shadow-chg-001-sideways": _v(sharpe=0.15),
        }
    )
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=loader,
    )
    result = oracle.validate(_make_change())
    assert result.passed, f"expected pass, got: {result.reason}"
    assert result.regimes_failed == []


# ------------------------------------------------------------- Tier-C firewall


@pytest.mark.parametrize(
    "constant,after",
    [
        ("MIN_RR_MULTIPLE", 2.0),        # lowering from 3.0 → looser
        ("MIN_KILL_THESIS_LEN", 20),     # lowering from 40 → looser
        ("MIN_NONZERO_LENSES", 1),       # lowering from 2 → looser
        ("MAX_CONFIDENCE", 0.99),        # raising from 0.9 → looser
        ("CONFIDENCE_RECONCILE_TOLERANCE", 0.20),  # raising → looser
    ],
)
def test_tier_c_loosening_via_constant_field_is_rejected(
    regime_loader, base_config, constant, after
):
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    change = _make_change(
        target="validate_take_rule",
        diff={"constant": constant, "before": None, "after": after},
    )
    with pytest.raises(ValueError, match="Tier-C"):
        oracle.validate(change)


def test_tier_c_loosening_via_path_is_rejected(regime_loader, base_config):
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    change = _make_change(
        target="config_threshold",
        diff={"path": ["signals", "MIN_RR_MULTIPLE"], "before": 3.0, "after": 2.0},
    )
    with pytest.raises(ValueError, match="MIN_RR_MULTIPLE"):
        oracle.validate(change)


def test_tier_c_tightening_is_allowed(regime_loader, base_config):
    """Raising MIN_RR_MULTIPLE beyond 3.0 tightens validate_take → allowed."""
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    change = _make_change(
        target="config_threshold",
        diff={"path": ["signals", "MIN_RR_MULTIPLE"], "before": 3.0, "after": 3.5},
    )
    # No raise; the change runs through the normal gate.
    result = oracle.validate(change)
    assert isinstance(result, OracleResult)


def test_tier_c_reference_without_numeric_after_is_rejected(regime_loader, base_config):
    """A Tier-C reference lacking a numeric 'after' is suspect → reject."""
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    change = _make_change(
        target="validate_take_rule",
        diff={"constant": "MAX_CONFIDENCE", "text": "increase to something"},
    )
    with pytest.raises(ValueError, match="Tier-C"):
        oracle.validate(change)


def test_non_tier_c_change_flows_through_gate(regime_loader, base_config):
    """Ordinary Tier-A cooldown tuning is NOT a Tier-C constant → no firewall trip."""
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    change = _make_change(
        target="config_threshold",
        diff={"path": ["GATE", "cooldown_seconds"], "before": 300, "after": 900},
    )
    result = oracle.validate(change)
    assert result.passed  # normal happy path


# ------------------------------------------------------------- edge cases


def test_missing_held_out_candles_raises(regime_loader, base_config):
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=None,
        regime_loader=regime_loader,
    )
    with pytest.raises(RuntimeError, match="held_out_candles"):
        oracle.validate(_make_change())


def test_empty_held_out_candles_raises(regime_loader, base_config):
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"]
        ),
        regime_loader=regime_loader,
    )
    with pytest.raises(RuntimeError, match="held_out_candles"):
        oracle.validate(_make_change())


def test_missing_regime_archive_surfaces_as_runtime_error(base_config, tmp_path):
    """Loader with no fixtures on disk → RuntimeError with actionable message."""
    loader = RegimeArchiveLoader(root=tmp_path)  # empty dir
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=loader,
    )
    with pytest.raises(RuntimeError, match="regime archive"):
        oracle.validate(_make_change())


def test_no_regime_loader_skips_regime_tests(base_config):
    """Regime testing is optional — omit loader → regimes_tested is empty."""
    harness = FakeHarness(
        {
            "baseline-": _v(sharpe=0.5),
            "shadow-": _v(sharpe=0.8),
        }
    )
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=None,
    )
    result = oracle.validate(_make_change())
    assert result.regimes_tested == []
    assert result.regimes_failed == []
    assert result.passed


def test_oracle_records_change_id(regime_loader, base_config):
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    change = _make_change(change_id="chg-abc")
    result = oracle.validate(change)
    assert result.change_id == "chg-abc"


def test_oracle_metrics_shape(regime_loader, base_config):
    harness = FakeHarness({"baseline-": _v(sharpe=0.5), "shadow-": _v(sharpe=0.8)})
    oracle = BacktestOracle(
        backtest_harness=harness,
        base_config=base_config,
        held_out_candles=_tiny_candles(),
        regime_loader=regime_loader,
    )
    result = oracle.validate(_make_change())
    for key in ("sharpe", "win_rate", "max_dd", "dsr", "pbo", "ruin_prob"):
        assert key in result.metrics, f"missing metric: {key}"
        assert key in result.baseline_metrics


# ------------------------------------------------------------- integration smoke


def test_oracle_runs_end_to_end_against_real_runner(regime_loader, base_config):
    """Smoke: hand the oracle a real BacktestRunner (not a fake) and confirm it wires up.

    This is a shape-only assertion — pass/fail depends on the strategy, and
    the trivial strategy below won't reliably pass the promotion gate.
    """
    from src.backtest.runner import BacktestRunner
    from src.backtest.strategy import Bar, Order, PositionState

    class DoNothing:
        def __init__(self, hypothesis=None):
            self.hypothesis = hypothesis or {}

        def on_bar(self, bar, feature_pack, position_state):
            return None

    runner = BacktestRunner(
        strategy_factory=lambda h: DoNothing(h),
        walk_forward_geometry=(120, 40, 40),
        cpcv_params={"n_groups": 4, "k_test": 2, "embargo_pct": 0.0, "label_horizon_bars": 3},
    )
    oracle = BacktestOracle(
        backtest_harness=runner,
        base_config=base_config,
        held_out_candles=_tiny_candles(300),
        regime_loader=regime_loader,
    )
    result = oracle.validate(_make_change())
    assert isinstance(result, OracleResult)
    # A do-nothing strategy trades zero → the ruin proxy pins at 1.0 for
    # both sides, so the ruin gate does not trip; sharpe is 0 for both,
    # sharpe delta = 0 which fails the improvement gate → passed=False
    # is the expected outcome.
    assert result.passed is False
    assert "Sharpe delta" in result.reason
