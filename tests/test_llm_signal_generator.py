"""Tests for the LLM-driven signal generator."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.llm.atlas_brain import AtlasBrain, BrainConfig
from src.llm.schema import (
    ConfidenceBreakdown,
    EntryZone,
    SituationReport,
    TradeProposal,
)
from src.memory.markdown_store import MarkdownMemory
from src.risk.risk_calculator import RiskCalculator
from src.signals.llm_signal_generator import LLMSignalGenerator


def _candle(ts, op, hi, lo, cl, vol=1000):
    return {"timestamp": ts, "open": op, "high": hi, "low": lo, "close": cl, "volume": vol}


def _flat_candles(price, count, vol=1000):
    return [_candle(i * 60_000, price, price + 0.5, price - 0.5, price, vol) for i in range(count)]


def _example_take_sr(price: float = 2042.0) -> SituationReport:
    return SituationReport(
        timestamp="2026-05-02T18:14:00Z",
        instrument="XAUUSD",
        data_quality="FRESH",
        regime="accumulation",
        evidence=["CVD divergence", "BOS confirmed"],
        trapped_party=None,
        dominant_party=None,
        asymmetry="Late shorts caught at 2055 sweep, no bid below.",
        thesis="Mean revert from spike low.",
        trade_proposal=TradeProposal(
            decision="TAKE",
            direction="long",
            entry_zone=EntryZone(low=price - 0.5, high=price + 0.5),
            invalidation="M5 close below 2040",
            first_target=price * 1.003,
            rr_minimum=3.0,
            size_pct=2.0,
        ),
        kill_thesis="If M5 close < 2040 with CVD positive, exit at market.",
        confidence=0.72,
        confidence_breakdown=ConfidenceBreakdown(flow=0.20, structure=0.18, context=0.18, intent=0.16),
        notes_to_future_atlas="Watch NY open replay.",
    )


def _no_trade_sr() -> SituationReport:
    sr = _example_take_sr()
    sr.trade_proposal = TradeProposal(
        decision="NO_TRADE",
        direction="n/a",
        entry_zone=None,
        invalidation="n/a",
        first_target=None,
        rr_minimum=0.0,
        size_pct=0.0,
    )
    return sr


@pytest.fixture
def memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield MarkdownMemory(root=Path(tmpdir))


@pytest.fixture
def fake_brain():
    """A brain stub whose `analyze` is a MagicMock; tests set return_value."""
    brain = MagicMock(spec=AtlasBrain)
    return brain


@pytest.fixture
def gen(fake_brain, memory):
    risk = RiskCalculator(account_risk_pct=2.0, sl_distance_pips=25, tp_distances_pips=[15, 30, 60])
    return LLMSignalGenerator(
        brain=fake_brain,
        memory=memory,
        risk_calc=risk,
        instrument="XAUUSD",
        min_signal_interval_seconds=0,
    )


def test_build_feature_pack_has_all_four_lenses(gen):
    candles = _flat_candles(2000, 50)
    pack = gen.build_feature_pack(candles, candles, candles, candles)
    assert "lens_flow" in pack
    assert "lens_structure" in pack
    assert "lens_context" in pack
    assert "lens_intent" in pack
    assert pack["instrument"] == "XAUUSD"
    assert pack["latest_price"]["m5_close"] == 2000


def test_generate_returns_situation_report(gen, fake_brain):
    fake_brain.analyze.return_value = _example_take_sr()
    candles = _flat_candles(2042, 50)
    pack = gen.build_feature_pack(candles, candles, candles, candles)
    sr = gen.generate(pack)
    assert sr is not None
    assert sr.trade_proposal.decision == "TAKE"


def test_take_sr_converts_to_trade_signal(gen, fake_brain):
    sr = _example_take_sr(price=2042.0)
    # Strengthen the SR enough to clear validate_take()
    sr.trapped_party = type(sr.trapped_party).__class__ if sr.trapped_party else None
    from src.llm.schema import ConfidenceBreakdown, TrappedParty
    sr.trapped_party = TrappedParty(who="late longs", level=2055.0, pain_bp=38.0)
    sr.kill_thesis = (
        "If M5 closes back above 2055 with CVD positive, the thesis is dead "
        "and we exit at market."
    )
    sr.confidence = 0.72
    sr.confidence_breakdown = ConfidenceBreakdown(
        flow=0.20, structure=0.18, context=0.18, intent=0.16
    )
    signal = gen.situation_report_to_trade_signal(sr, account_balance=10_000)
    assert signal is not None
    assert 2041.0 <= signal.entry_price <= 2043.0
    assert signal.stop_loss < signal.entry_price
    assert signal.tp1 > signal.entry_price
    # Brain's first_target should override the rules-based TP1
    assert abs(signal.tp1 - sr.trade_proposal.first_target) < 1e-6
    assert signal.confidence <= 0.9
    assert signal.direction == "long"


def test_no_trade_sr_does_not_convert(gen, fake_brain):
    sr = _no_trade_sr()
    signal = gen.situation_report_to_trade_signal(sr, account_balance=10_000)
    assert signal is None


def test_converter_rejects_when_position_already_open(gen, fake_brain):
    """Law 11 — one bullet, one chamber."""
    from src.llm.schema import ConfidenceBreakdown, TrappedParty
    sr = _example_take_sr(price=2042.0)
    sr.trapped_party = TrappedParty(who="late longs", level=2055.0, pain_bp=38.0)
    sr.kill_thesis = (
        "If M5 closes back above 2055 with CVD positive, the thesis is dead and we exit."
    )
    sr.confidence = 0.72
    sr.confidence_breakdown = ConfidenceBreakdown(flow=0.2, structure=0.18, context=0.18, intent=0.16)
    signal = gen.situation_report_to_trade_signal(
        sr, account_balance=10_000, has_open_position=True
    )
    assert signal is None


def test_min_interval_blocks_repeat_calls(fake_brain, memory):
    risk = RiskCalculator(account_risk_pct=2.0, sl_distance_pips=25, tp_distances_pips=[15, 30, 60])
    gen = LLMSignalGenerator(
        brain=fake_brain,
        memory=memory,
        risk_calc=risk,
        instrument="XAUUSD",
        min_signal_interval_seconds=3600,
    )
    fake_brain.analyze.return_value = _example_take_sr()
    candles = _flat_candles(2042, 50)
    pack = gen.build_feature_pack(candles, candles, candles, candles)
    first = gen.generate(pack)
    second = gen.generate(pack)  # immediately again
    assert first is not None
    assert second is None  # blocked by interval
    assert fake_brain.analyze.call_count == 1


def test_force_bypasses_interval(fake_brain, memory):
    risk = RiskCalculator(account_risk_pct=2.0, sl_distance_pips=25, tp_distances_pips=[15, 30, 60])
    gen = LLMSignalGenerator(
        brain=fake_brain,
        memory=memory,
        risk_calc=risk,
        instrument="XAUUSD",
        min_signal_interval_seconds=3600,
    )
    fake_brain.analyze.return_value = _example_take_sr()
    candles = _flat_candles(2042, 50)
    pack = gen.build_feature_pack(candles, candles, candles, candles)
    gen.generate(pack)
    forced = gen.generate(pack, force=True)
    assert forced is not None
    assert fake_brain.analyze.call_count == 2
