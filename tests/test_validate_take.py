"""Tests for the validate_take chokepoint — the doctrine enforcement gate."""
from __future__ import annotations

import pytest

from src.llm.schema import (
    ConfidenceBreakdown,
    EntryZone,
    SituationReport,
    TradeProposal,
    TrappedParty,
)
from src.signals.llm_signal_generator import (
    MAX_CONFIDENCE,
    MIN_KILL_THESIS_LEN,
    MIN_NONZERO_LENSES,
    MIN_RR_MULTIPLE,
    validate_take,
)


def _good_sr(**overrides):
    """Build a TAKE SR that should pass validation. Overrides shallow-merge in."""
    base = dict(
        timestamp="2026-05-02T18:14:00Z",
        instrument="XAUUSD",
        data_quality="FRESH",
        regime="accumulation",
        evidence=["evidence A", "evidence B", "evidence C"],
        trapped_party=TrappedParty(who="late longs from 14:32 sweep", level=2055.0, pain_bp=38.0),
        dominant_party=None,
        asymmetry="late longs trapped above 2055 with no bid",
        thesis="thesis text",
        trade_proposal=TradeProposal(
            decision="TAKE",
            direction="long",
            entry_zone=EntryZone(low=2041.5, high=2042.0),
            invalidation="M5 close back above 2055",
            first_target=2050.0,
            rr_minimum=3.0,
            size_pct=2.0,
        ),
        kill_thesis="If price closes above 2055 on M15 with CVD positive, the thesis is dead and we exit at market.",
        confidence=0.72,
        confidence_breakdown=ConfidenceBreakdown(flow=0.20, structure=0.18, context=0.18, intent=0.16),
        notes_to_future_atlas="watch NY open",
    )
    base.update(overrides)
    return SituationReport(**base)


def test_valid_take_passes():
    assert validate_take(_good_sr()) is None


def test_no_trade_short_circuits():
    sr = _good_sr()
    sr.trade_proposal.decision = "NO_TRADE"
    assert validate_take(sr) is None  # caller handles non-TAKE


def test_skip_short_circuits():
    sr = _good_sr()
    sr.trade_proposal.decision = "SKIP"
    assert validate_take(sr) is None


def test_law_2_missing_trapped_party_rejects():
    reason = validate_take(_good_sr(trapped_party=None))
    assert "Law 2" in reason


def test_law_3_short_kill_thesis_rejects():
    reason = validate_take(_good_sr(kill_thesis="exit if bad"))
    assert "Law 3" in reason


def test_law_3_vague_kill_thesis_rejects():
    sr = _good_sr(kill_thesis="If it goes against me I will exit immediately at market.")
    reason = validate_take(sr)
    assert "Law 3" in reason


def test_law_3_vibes_kill_thesis_rejects():
    sr = _good_sr(kill_thesis="Trust the gut feel and bail when vibes feel off, very serious.")
    reason = validate_take(sr)
    assert "Law 3" in reason


def test_law_4_low_rr_rejects():
    sr = _good_sr()
    sr.trade_proposal.rr_minimum = 2.0
    reason = validate_take(sr)
    assert "Law 4" in reason


def test_law_5_breakdown_must_sum_to_confidence():
    sr = _good_sr(
        confidence=0.80,
        confidence_breakdown=ConfidenceBreakdown(flow=0.05, structure=0.05, context=0.05, intent=0.05),
    )
    reason = validate_take(sr)
    assert "Law 5" in reason


def test_law_6_confidence_above_cap_rejects():
    sr = _good_sr(
        confidence=0.95,
        confidence_breakdown=ConfidenceBreakdown(flow=0.25, structure=0.24, context=0.23, intent=0.23),
    )
    reason = validate_take(sr)
    assert "Law 6" in reason


def test_law_7_only_one_lens_active_rejects():
    sr = _good_sr(
        confidence=0.20,
        confidence_breakdown=ConfidenceBreakdown(flow=0.20, structure=0.0, context=0.0, intent=0.0),
    )
    reason = validate_take(sr)
    assert "Law 7" in reason


def test_law_8_stale_data_rejects():
    reason = validate_take(_good_sr(data_quality="STALE"))
    assert "Law 8" in reason


def test_law_8_degraded_data_rejects():
    reason = validate_take(_good_sr(data_quality="DEGRADED"))
    assert "Law 8" in reason


def test_law_11_open_position_rejects():
    reason = validate_take(_good_sr(), has_open_position=True)
    assert "Law 11" in reason


def test_take_without_entry_zone_rejects():
    sr = _good_sr()
    sr.trade_proposal.entry_zone = None
    reason = validate_take(sr)
    assert "entry_zone" in reason.lower() or "Schema" in reason
