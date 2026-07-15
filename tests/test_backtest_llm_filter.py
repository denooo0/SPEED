"""Tests for LLMFilterStrategy — the Path A brain-as-filter wrapper.

FakeBrain uses pre-scripted SituationReports so no network calls happen and
tests run in well under 30s.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from src.backtest.cost_model import CostModel
from src.backtest.cost_telemetry import CostTracker
from src.backtest.llm_filter import LLMDecision, LLMFilterStrategy
from src.backtest.simulator import EventDrivenSimulator
from src.backtest.strategy import Bar, Order, PositionState
from src.llm.schema import (
    ConfidenceBreakdown,
    EntryZone,
    SituationReport,
    TradeProposal,
    TrappedParty,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 200
    cache_read_input_tokens: int = 4000
    cache_creation_input_tokens: int = 0


@dataclass
class _FakeResponse:
    usage: _FakeUsage


class _FakeBrainConfig:
    model = "claude-opus-4-7"


class FakeBrain:
    """Deterministic stand-in for AtlasBrain. Returns pre-scripted SRs.

    Usage:
        brain = FakeBrain([sr1, sr2, sr3])   # in call order
    OR
        brain = FakeBrain(lambda pack, digest, setups: sr)

    Records every call in ``.calls`` for assertions. Also mimics AtlasBrain's
    ``.config.model`` attribute so cost telemetry can price correctly.
    """

    def __init__(
        self,
        script: Any,
        usage: Optional[_FakeUsage] = None,
    ) -> None:
        self._script = script
        self._usage = usage or _FakeUsage()
        self.calls: List[Dict[str, Any]] = []
        self.config = _FakeBrainConfig()
        # Attach a fake usage on the last-issued response so LLMFilterStrategy
        # can pull tokens back.
        self.last_response: Optional[_FakeResponse] = None

    def analyze(
        self,
        feature_pack: Dict[str, Any],
        memory_digest: str,
        relevant_setups: Optional[List[str]] = None,
    ) -> SituationReport:
        self.calls.append({
            "feature_pack": feature_pack,
            "memory_digest": memory_digest,
            "relevant_setups": relevant_setups,
        })
        if callable(self._script):
            sr = self._script(feature_pack, memory_digest, relevant_setups)
        elif isinstance(self._script, list):
            idx = min(len(self.calls) - 1, len(self._script) - 1)
            sr = self._script[idx]
        else:
            sr = self._script
        # AtlasBrain returns just the parsed SR, but we need to expose usage
        # so CostTracker can read it. LLMFilterStrategy accesses response.usage
        # via getattr; SR itself has no `.usage` attribute, so we monkey-patch
        # one onto the returned object.
        try:
            object.__setattr__(sr, "usage", self._usage)
        except Exception:
            pass
        return sr


def _take_sr(**overrides: Any) -> SituationReport:
    """A well-formed TAKE SR that should pass validate_take."""
    base = dict(
        timestamp="2026-05-02T18:14:00Z",
        instrument="XAUUSD",
        data_quality="FRESH",
        regime="accumulation",
        evidence=["e1", "e2", "e3"],
        trapped_party=TrappedParty(who="late longs", level=2055.0, pain_bp=38.0),
        dominant_party=None,
        asymmetry="late longs trapped above 2055 with no bid",
        thesis="mean revert",
        trade_proposal=TradeProposal(
            decision="TAKE",
            direction="long",
            entry_zone=EntryZone(low=2041.5, high=2042.0),
            invalidation="M5 close back above 2055",
            first_target=2050.0,
            rr_minimum=3.0,
            size_pct=2.0,
        ),
        kill_thesis="If price closes above 2055 on M15 with CVD positive, the thesis is dead — exit at market.",
        confidence=0.72,
        confidence_breakdown=ConfidenceBreakdown(flow=0.20, structure=0.18, context=0.18, intent=0.16),
        notes_to_future_atlas="watch NY open",
    )
    base.update(overrides)
    return SituationReport(**base)


def _no_trade_sr() -> SituationReport:
    sr = _take_sr()
    sr.trade_proposal.decision = "NO_TRADE"
    return sr


def _skip_sr() -> SituationReport:
    sr = _take_sr()
    sr.trade_proposal.decision = "SKIP"
    return sr


def _invalid_take_sr() -> SituationReport:
    """A TAKE that will fail validate_take (Law 4: rr_minimum too low)."""
    sr = _take_sr()
    sr.trade_proposal.rr_minimum = 1.5
    return sr


# ---------------------------------------------------------------------------
# Base strategies
# ---------------------------------------------------------------------------


class _AlwaysProposesLong:
    """Emits a long order on EVERY flat bar."""

    def on_bar(self, bar: Bar, feature_pack: Dict[str, Any], position_state: PositionState) -> Optional[Order]:
        if position_state.is_open:
            return None
        return Order(
            direction="long",
            entry_price=bar.close,
            stop_loss=bar.close * 0.9,
            take_profits=[bar.close * 1.1],
            size=1.0,
        )


class _NeverProposes:
    def on_bar(self, bar, fp, ps):
        return None


def _synthetic_candles(n_bars: int = 12) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n_bars, freq="5min")
    closes = np.linspace(100.0, 105.0, n_bars)
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


def test_pass_through_when_brain_is_none_matches_base_exactly():
    """brain=None → wrapper.decide is a no-op layer over base_strategy."""
    df = _synthetic_candles()
    base = _AlwaysProposesLong()
    wrapper = LLMFilterStrategy(base_strategy=base, brain=None)

    cm = CostModel(
        spread_bps_normal=0.0,
        slippage_bps_normal=0.0,
        fee_bps_taker=0.0,
        funding_bps_per_8h=0.0,
        latency_seconds=0.0,
    )
    sim_base = EventDrivenSimulator(df, cm)
    sim_wrapped = EventDrivenSimulator(df, cm)
    res_base = sim_base.run(_AlwaysProposesLong(), [{} for _ in range(len(df))])
    res_wrap = sim_wrapped.run(wrapper, [{} for _ in range(len(df))])

    # Same trade count and same PnL sequence.
    assert res_base.n_trades == res_wrap.n_trades
    assert len(res_base.trades) == len(res_wrap.trades)
    if not res_base.trades.empty:
        # Trade-level identity: same entries/exits/pnls.
        for col in ("entry_price", "exit_price", "pnl"):
            assert (res_base.trades[col].to_numpy() == pytest.approx(res_wrap.trades[col].to_numpy()))
    # Equity curves match numerically.
    assert (res_base.equity_curve.to_numpy() == pytest.approx(res_wrap.equity_curve.to_numpy()))


def test_pass_through_never_calls_brain_analyze():
    df = _synthetic_candles(n_bars=6)
    brain = FakeBrain(_take_sr())
    # Even though a brain is *available*, forcing brain=None still skips it.
    wrapper = LLMFilterStrategy(_AlwaysProposesLong(), brain=None)
    sim = EventDrivenSimulator(df, CostModel())
    sim.run(wrapper, [{} for _ in range(len(df))])
    assert len(brain.calls) == 0


def test_brain_take_approves_order():
    df = _synthetic_candles(n_bars=6)
    brain = FakeBrain(_take_sr())
    tracker = CostTracker()
    wrapper = LLMFilterStrategy(
        _AlwaysProposesLong(),
        brain=brain,
        cost_tracker=tracker,
        cache_ttl_bars=0,   # disable caching so every proposal hits the brain
    )

    sim = EventDrivenSimulator(df, CostModel())
    res = sim.run(wrapper, [{} for _ in range(len(df))])
    # At least one trade got placed (base proposed on every flat bar).
    assert res.n_trades >= 1
    # Brain was called on every flat bar the base proposed.
    assert len(brain.calls) >= 1
    # Cost tracker charged.
    assert tracker.total_calls == len(brain.calls)


def test_brain_no_trade_filters_out_all_orders():
    df = _synthetic_candles(n_bars=6)
    brain = FakeBrain(_no_trade_sr())
    tracker = CostTracker()
    wrapper = LLMFilterStrategy(
        _AlwaysProposesLong(),
        brain=brain,
        cost_tracker=tracker,
        cache_ttl_bars=0,
    )
    sim = EventDrivenSimulator(df, CostModel())
    res = sim.run(wrapper, [{} for _ in range(len(df))])
    assert res.n_trades == 0
    assert wrapper.filtered_out_count >= 1
    assert wrapper.approved_count == 0


def test_brain_take_but_validate_rejects_filters_out():
    """When brain says TAKE but validate_take rejects, the order is filtered out."""
    df = _synthetic_candles(n_bars=6)
    brain = FakeBrain(_invalid_take_sr())
    wrapper = LLMFilterStrategy(_AlwaysProposesLong(), brain=brain, cache_ttl_bars=0)
    sim = EventDrivenSimulator(df, CostModel())
    res = sim.run(wrapper, [{} for _ in range(len(df))])
    assert res.n_trades == 0
    assert wrapper.filtered_out_count >= 1
    # Every decision should have a validate_take reason.
    for d in wrapper.decisions:
        if d.reason.startswith("validate_take"):
            assert "Law 4" in d.reason
            break
    else:
        pytest.fail("expected at least one decision rejected by validate_take")


def test_cache_prevents_duplicate_brain_calls_on_identical_setup():
    """Same bar + same position state twice → only one brain call."""
    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000.0)
    pos = PositionState()  # flat
    base = _AlwaysProposesLong()
    brain = FakeBrain(_take_sr())
    wrapper = LLMFilterStrategy(base, brain=brain, cache_ttl_bars=100)

    # Two identical (bar, position) inputs.
    out1 = wrapper.on_bar(bar, {}, pos)
    out2 = wrapper.on_bar(bar, {}, pos)

    assert out1 is not None
    assert out2 is not None
    # Brain must have been called exactly once.
    assert len(brain.calls) == 1
    # cache_hit_rate on 2 decisions = 1/2 = 0.5
    assert wrapper.cache_hit_rate() == pytest.approx(0.5)


def test_cache_hit_rate_at_least_50pct_on_repeats():
    """Real-world: N identical proposals → cache hits saturate above 50%."""
    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000.0)
    pos = PositionState()
    brain = FakeBrain(_take_sr())
    wrapper = LLMFilterStrategy(_AlwaysProposesLong(), brain=brain, cache_ttl_bars=100)

    # Ten identical calls → 1 miss + 9 hits = 90% hit rate.
    for _ in range(10):
        wrapper.on_bar(bar, {}, pos)
    assert wrapper.cache_hit_rate() >= 0.5
    assert wrapper.cache_hit_rate() == pytest.approx(0.9)
    assert len(brain.calls) == 1  # only the first call hit the API


def test_cache_ttl_bars_zero_disables_cache():
    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000.0)
    pos = PositionState()
    brain = FakeBrain(_take_sr())
    wrapper = LLMFilterStrategy(_AlwaysProposesLong(), brain=brain, cache_ttl_bars=0)

    for _ in range(3):
        wrapper.on_bar(bar, {}, pos)
    assert len(brain.calls) == 3
    assert wrapper.cache_hit_rate() == 0.0


def test_cache_key_differs_when_position_state_changes():
    """Same bar, different open/flat position → separate cache entries."""
    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000.0)
    brain = FakeBrain(_take_sr())
    wrapper = LLMFilterStrategy(_AlwaysProposesLong(), brain=brain, cache_ttl_bars=100)

    flat = PositionState(is_open=False)
    opened = PositionState(is_open=True, direction="long", entry_price=100.0, size=1.0, bars_held=3)

    wrapper.on_bar(bar, {}, flat)
    wrapper.on_bar(bar, {}, opened)
    # opened → base won't propose because is_open=True. But the wrapper still
    # skips the brain call in that case (base returned None). So we expect
    # exactly 1 brain call from the flat lookup.
    assert len(brain.calls) == 1


def test_brain_error_falls_back_to_skip():
    """Exceptions from brain.analyze → decision is SKIP, no crash."""
    class Boom:
        config = _FakeBrainConfig()

        def analyze(self, *a, **kw):
            raise RuntimeError("api down")

    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000.0)
    wrapper = LLMFilterStrategy(_AlwaysProposesLong(), brain=Boom(), cache_ttl_bars=0)
    out = wrapper.on_bar(bar, {}, PositionState())
    assert out is None
    assert wrapper.decisions[-1].action == "SKIP"
    assert "brain error" in wrapper.decisions[-1].reason


def test_stats_shape():
    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000.0)
    brain = FakeBrain(_take_sr())
    wrapper = LLMFilterStrategy(_AlwaysProposesLong(), brain=brain)
    wrapper.on_bar(bar, {}, PositionState())
    stats = wrapper.stats()
    for key in (
        "n_decisions",
        "approved",
        "filtered_out",
        "cache_hits",
        "cache_misses",
        "cache_hit_rate",
        "brain_calls",
        "estimated_usd",
    ):
        assert key in stats


def test_feature_replayer_output_reaches_brain():
    """When a replayer is supplied, its rich pack replaces the shallow dict."""

    class _FakeReplayer:
        def __init__(self):
            self.calls = 0

        def pack_at(self, ts):
            self.calls += 1
            # Return an object with to_dict(), mimicking FeaturePack.
            class _Pack:
                def to_dict(self_):
                    return {"replayer_pack": True, "ts": str(ts)}
            return _Pack()

    replayer = _FakeReplayer()
    brain = FakeBrain(_take_sr())
    wrapper = LLMFilterStrategy(
        _AlwaysProposesLong(),
        brain=brain,
        feature_replayer=replayer,
        cache_ttl_bars=0,
    )
    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000.0)
    wrapper.on_bar(bar, {"shallow": True}, PositionState())

    # Brain must have received the replayer's rich pack, not the shallow dict.
    assert replayer.calls == 1
    assert brain.calls[0]["feature_pack"].get("replayer_pack") is True


def test_decide_2arg_signature_uses_last_feature_pack():
    """The public 2-arg decide() reuses whatever feature_pack on_bar last saw."""
    brain = FakeBrain(_take_sr())
    wrapper = LLMFilterStrategy(_AlwaysProposesLong(), brain=brain, cache_ttl_bars=0)
    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar1 = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)
    bar2 = Bar(timestamp=ts + pd.Timedelta(minutes=5), open=100.5, high=101.5, low=100.0, close=101.0, volume=1000.0)

    # First: on_bar populates _last_feature_pack.
    wrapper.on_bar(bar1, {"context": "morning"}, PositionState())
    # Then: decide() with just 2 args must still work.
    out = wrapper.decide(bar2, PositionState())
    assert out is not None
    # Two brain calls happened (different bars → different cache keys).
    assert len(brain.calls) == 2


def test_cost_tracker_records_tokens_from_response():
    """Every brain call charges the shared CostTracker."""
    brain = FakeBrain(_take_sr(), usage=_FakeUsage(input_tokens=333, output_tokens=555))
    tracker = CostTracker()
    wrapper = LLMFilterStrategy(
        _AlwaysProposesLong(), brain=brain, cost_tracker=tracker, cache_ttl_bars=0
    )
    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000.0)
    wrapper.on_bar(bar, {}, PositionState())
    assert tracker.total_calls == 1
    assert tracker.total_input_tokens == 333
    assert tracker.total_output_tokens == 555


def test_no_brain_call_when_base_proposes_nothing():
    """If base returns None, brain must NOT be invoked (cost saving)."""
    brain = FakeBrain(_take_sr())
    wrapper = LLMFilterStrategy(_NeverProposes(), brain=brain, cache_ttl_bars=0)
    df = _synthetic_candles(n_bars=6)
    sim = EventDrivenSimulator(df, CostModel())
    sim.run(wrapper, [{} for _ in range(len(df))])
    assert len(brain.calls) == 0
    assert wrapper.filtered_out_count == 0
    assert wrapper.approved_count == 0


def test_decision_recorded_with_tokens_and_cache_flag():
    brain = FakeBrain(_take_sr(), usage=_FakeUsage(input_tokens=10, output_tokens=20))
    wrapper = LLMFilterStrategy(_AlwaysProposesLong(), brain=brain, cache_ttl_bars=100)
    ts = pd.Timestamp("2024-01-01 09:00:00")
    bar = Bar(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000.0)
    pos = PositionState()
    wrapper.on_bar(bar, {}, pos)
    wrapper.on_bar(bar, {}, pos)  # cache hit

    d1, d2 = wrapper.decisions[0], wrapper.decisions[1]
    assert d1.cached is False
    assert d1.tokens_used["input_tokens"] == 10
    assert d2.cached is True
    # tokens_used preserved on the copy.
    assert d2.tokens_used["input_tokens"] == 10
