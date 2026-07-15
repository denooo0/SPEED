"""Tests for the A/B backtest harness (Path A filter vs deterministic-only)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from src.backtest.ab_harness import ABBacktestHarness, ABResult
from src.backtest.cost_model import CostModel
from src.backtest.strategy import Bar, Order, PositionState
from src.llm.schema import (
    ConfidenceBreakdown,
    EntryZone,
    SituationReport,
    TradeProposal,
    TrappedParty,
)


# ---------------------------------------------------------------------------
# Test helpers (mirror those in test_backtest_llm_filter.py, kept local so
# the two test files can be run in isolation)
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 200
    cache_read_input_tokens: int = 4000
    cache_creation_input_tokens: int = 0


class _FakeBrainConfig:
    model = "claude-opus-4-7"


class FakeBrain:
    def __init__(self, script: Any, usage: Optional[_FakeUsage] = None) -> None:
        self._script = script
        self._usage = usage or _FakeUsage()
        self.calls: List[Dict[str, Any]] = []
        self.config = _FakeBrainConfig()

    def analyze(
        self,
        feature_pack: Dict[str, Any],
        memory_digest: str,
        relevant_setups: Optional[List[str]] = None,
    ) -> SituationReport:
        self.calls.append({"pack": feature_pack, "digest": memory_digest})
        if callable(self._script):
            sr = self._script(feature_pack, memory_digest, relevant_setups)
        elif isinstance(self._script, list):
            idx = min(len(self.calls) - 1, len(self._script) - 1)
            sr = self._script[idx]
        else:
            sr = self._script
        try:
            object.__setattr__(sr, "usage", self._usage)
        except Exception:
            pass
        return sr


def _take_sr(**overrides: Any) -> SituationReport:
    base = dict(
        timestamp="2026-05-02T18:14:00Z",
        instrument="XAUUSD",
        data_quality="FRESH",
        regime="accumulation",
        evidence=["e1", "e2"],
        trapped_party=TrappedParty(who="late longs", level=100.0, pain_bp=38.0),
        dominant_party=None,
        asymmetry="late longs trapped",
        thesis="mean revert",
        trade_proposal=TradeProposal(
            decision="TAKE",
            direction="long",
            entry_zone=EntryZone(low=100.0, high=101.0),
            invalidation="close back above 105",
            first_target=110.0,
            rr_minimum=3.0,
            size_pct=2.0,
        ),
        kill_thesis="If M5 closes above 105 with CVD positive the thesis is dead, exit at market.",
        confidence=0.72,
        confidence_breakdown=ConfidenceBreakdown(flow=0.20, structure=0.18, context=0.18, intent=0.16),
        notes_to_future_atlas="watch NY",
    )
    base.update(overrides)
    return SituationReport(**base)


def _no_trade_sr() -> SituationReport:
    sr = _take_sr()
    sr.trade_proposal.decision = "NO_TRADE"
    return sr


class _AlwaysProposesLongEveryNBars:
    """Proposes a long every N bars when flat. Deterministic + stateless-ish."""

    def __init__(self, every: int = 3, sl_pct: float = 0.02, tp_pct: float = 0.05) -> None:
        self.every = int(every)
        self.sl_pct = float(sl_pct)
        self.tp_pct = float(tp_pct)
        self._counter = 0

    def on_bar(self, bar: Bar, feature_pack: Dict[str, Any], position_state: PositionState) -> Optional[Order]:
        self._counter += 1
        if position_state.is_open:
            return None
        if self._counter % self.every != 0:
            return None
        return Order(
            direction="long",
            entry_price=bar.close,
            stop_loss=bar.close * (1.0 - self.sl_pct),
            take_profits=[bar.close * (1.0 + self.tp_pct)],
            size=1.0,
        )


def _synthetic_candles(n_bars: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n_bars, freq="5min")
    rng = np.random.default_rng(42)
    closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0002, 0.003, n_bars))
    opens = np.empty(n_bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.001,
            "low": np.minimum(opens, closes) * 0.999,
            "close": closes,
            "volume": np.full(n_bars, 100.0),
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pass_through_matches_baseline_exactly():
    """brain=None → filtered arm identical to baseline."""
    df = _synthetic_candles(n_bars=40)
    harness = ABBacktestHarness(
        cost_model=CostModel(),
        brain=None,
        initial_equity_usd=500.0,
    )
    result = harness.run(
        base_strategy=_AlwaysProposesLongEveryNBars(every=3),
        candles=df,
        strategy_factory=lambda: _AlwaysProposesLongEveryNBars(every=3),
    )

    # Identical trade count.
    assert result.baseline.n_trades == result.filtered.n_trades
    # Identical PnL sequences.
    if not result.baseline.trades.empty:
        assert result.baseline.trades["pnl"].to_numpy() == pytest.approx(
            result.filtered.trades["pnl"].to_numpy()
        )
    # Identical equity curves.
    assert result.baseline.equity_curve.to_numpy() == pytest.approx(
        result.filtered.equity_curve.to_numpy()
    )
    # Filter alpha is zero.
    assert result.filter_alpha == pytest.approx(0.0)
    assert result.llm_cost_usd == 0.0
    assert result.net_alpha_usd == pytest.approx(0.0)
    assert result.trades_filtered_out == 0


def test_brain_rejecting_all_produces_different_equity():
    """When brain vetoes every setup, filtered arm has zero trades."""
    df = _synthetic_candles(n_bars=40)
    brain = FakeBrain(_no_trade_sr())
    harness = ABBacktestHarness(
        cost_model=CostModel(),
        brain=brain,
    )
    result = harness.run(
        base_strategy=_AlwaysProposesLongEveryNBars(every=3),
        candles=df,
        strategy_factory=lambda: _AlwaysProposesLongEveryNBars(every=3),
    )

    # Baseline has trades; filtered has none.
    assert result.baseline.n_trades > 0
    assert result.filtered.n_trades == 0
    # Equity curves diverge → different terminal equity.
    assert result.baseline.equity_curve.iloc[-1] != result.filtered.equity_curve.iloc[-1]
    # trades_filtered_out is positive.
    assert result.trades_filtered_out > 0
    assert result.trades_approved == 0
    # Cost is non-zero because brain WAS called.
    assert result.llm_cost_usd > 0.0


def test_brain_approving_all_matches_baseline_in_trades_but_costs_money():
    """When brain approves every proposal, trade sequence matches, cost > 0."""
    df = _synthetic_candles(n_bars=40)
    brain = FakeBrain(_take_sr())
    harness = ABBacktestHarness(cost_model=CostModel(), brain=brain, cache_ttl_bars=0)
    result = harness.run(
        base_strategy=_AlwaysProposesLongEveryNBars(every=3),
        candles=df,
        strategy_factory=lambda: _AlwaysProposesLongEveryNBars(every=3),
    )

    # Same number of trades.
    assert result.baseline.n_trades == result.filtered.n_trades
    # PnL identical (SR is approved, and validate_take passes).
    if not result.baseline.trades.empty:
        assert result.baseline.trades["pnl"].to_numpy() == pytest.approx(
            result.filtered.trades["pnl"].to_numpy()
        )
    # filter_alpha ≈ 0 (both arms produce same trades).
    assert result.filter_alpha == pytest.approx(0.0, abs=1e-12)
    # But llm_cost_usd is positive — this is the honest ledger cost.
    assert result.llm_cost_usd > 0.0
    # And net_alpha_usd = 0 - cost = negative.
    assert result.net_alpha_usd < 0.0


def test_ab_result_shape():
    df = _synthetic_candles(n_bars=30)
    brain = FakeBrain(_take_sr())
    harness = ABBacktestHarness(cost_model=CostModel(), brain=brain)
    result = harness.run(
        base_strategy=_AlwaysProposesLongEveryNBars(every=5),
        candles=df,
        strategy_factory=lambda: _AlwaysProposesLongEveryNBars(every=5),
    )

    assert isinstance(result, ABResult)
    for attr in (
        "baseline",
        "filtered",
        "filter_alpha",
        "llm_cost_usd",
        "net_alpha_usd",
        "trades_filtered_out",
        "trades_approved",
        "cache_hit_rate",
        "cost_tracker",
    ):
        assert hasattr(result, attr)
    summary = result.summary()
    assert "filter_alpha" in summary
    assert "llm_cost_usd" in summary


def test_net_alpha_computation_correct():
    """net_alpha_usd = filter_alpha * initial_equity - llm_cost_usd."""
    df = _synthetic_candles(n_bars=30)
    # Alternate TAKE / NO_TRADE so half get filtered → produces non-zero alpha.
    scripted = [_take_sr(), _no_trade_sr(), _take_sr(), _no_trade_sr(), _take_sr()] * 20
    brain = FakeBrain(scripted)
    initial = 1_000.0
    harness = ABBacktestHarness(
        cost_model=CostModel(),
        brain=brain,
        initial_equity_usd=initial,
        cache_ttl_bars=0,  # bars aren't identical anyway but avoid cache masking
    )
    result = harness.run(
        base_strategy=_AlwaysProposesLongEveryNBars(every=3),
        candles=df,
        strategy_factory=lambda: _AlwaysProposesLongEveryNBars(every=3),
    )

    expected = result.filter_alpha * initial - result.llm_cost_usd
    assert result.net_alpha_usd == pytest.approx(expected)


def test_baseline_arm_never_calls_brain():
    """The baseline arm must be brain-free — otherwise A/B is not clean."""
    df = _synthetic_candles(n_bars=20)
    brain = FakeBrain(_take_sr())
    harness = ABBacktestHarness(cost_model=CostModel(), brain=brain)
    call_count_before = len(brain.calls)
    result = harness.run(
        base_strategy=_AlwaysProposesLongEveryNBars(every=5),
        candles=df,
        strategy_factory=lambda: _AlwaysProposesLongEveryNBars(every=5),
    )
    call_count_after = len(brain.calls)
    # Some calls happened (filtered arm), but if we split by baseline vs filtered
    # the baseline made zero calls. We can't split by arm from outside, so we
    # check indirectly: total calls should equal filtered arm calls (== approved+filtered_out
    # minus cached).
    filtered_arm_calls = result.trades_approved + result.trades_filtered_out
    # brain calls == misses (cache-aware). With cache_ttl_bars=100 default and
    # potentially repeated bars we'd have fewer calls than proposals; here
    # every bar's OHLC differs so misses == proposals.
    assert (call_count_after - call_count_before) <= filtered_arm_calls


def test_strategy_factory_used_for_isolation():
    """Confirms baseline and filtered arm each get a fresh strategy instance."""
    df = _synthetic_candles(n_bars=20)
    instances_created: List[_AlwaysProposesLongEveryNBars] = []

    def factory() -> _AlwaysProposesLongEveryNBars:
        s = _AlwaysProposesLongEveryNBars(every=5)
        instances_created.append(s)
        return s

    brain = FakeBrain(_take_sr())
    harness = ABBacktestHarness(cost_model=CostModel(), brain=brain)
    harness.run(
        base_strategy=_AlwaysProposesLongEveryNBars(every=5),
        candles=df,
        strategy_factory=factory,
    )
    # One for baseline, one for filtered.
    assert len(instances_created) == 2
    # And they are distinct objects.
    assert instances_created[0] is not instances_created[1]


def test_start_end_slicing_applied_to_both_arms():
    df = _synthetic_candles(n_bars=40)
    start = df.index[10]
    end = df.index[25]
    brain = FakeBrain(_take_sr())
    harness = ABBacktestHarness(cost_model=CostModel(), brain=brain)
    result = harness.run(
        base_strategy=_AlwaysProposesLongEveryNBars(every=3),
        candles=df,
        start=start,
        end=end,
        strategy_factory=lambda: _AlwaysProposesLongEveryNBars(every=3),
    )
    # Both equity curves span the same slice.
    assert result.baseline.equity_curve.index[0] == result.filtered.equity_curve.index[0]
    assert result.baseline.equity_curve.index[-1] == result.filtered.equity_curve.index[-1]
    assert result.baseline.equity_curve.index[0] >= start
    assert result.baseline.equity_curve.index[-1] <= end


def test_cache_hit_rate_reported():
    """Cache hit rate must appear in ABResult and reflect the wrapper's state."""
    df = _synthetic_candles(n_bars=20)
    brain = FakeBrain(_take_sr())
    harness = ABBacktestHarness(cost_model=CostModel(), brain=brain, cache_ttl_bars=100)
    result = harness.run(
        base_strategy=_AlwaysProposesLongEveryNBars(every=3),
        candles=df,
        strategy_factory=lambda: _AlwaysProposesLongEveryNBars(every=3),
    )
    assert 0.0 <= result.cache_hit_rate <= 1.0
    assert isinstance(result.filter_stats, dict)
