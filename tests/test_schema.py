"""Tests for the SITUATION REPORT schema and EntryZone validator."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.llm.schema import (
    ConfidenceBreakdown,
    EntryZone,
    SituationReport,
    TradeProposal,
    TrappedParty,
)


def test_entry_zone_accepts_valid_range():
    z = EntryZone(low=2041.5, high=2042.0)
    assert z.low <= z.high


def test_entry_zone_rejects_inverted_range():
    with pytest.raises(ValidationError):
        EntryZone(low=2042.0, high=2041.0)


def test_entry_zone_rejects_zero_or_negative():
    with pytest.raises(ValidationError):
        EntryZone(low=0.0, high=10.0)
    with pytest.raises(ValidationError):
        EntryZone(low=-1.0, high=1.0)


def test_entry_zone_allows_low_equals_high():
    """Single-price entry is technically valid — no zone, just a level."""
    z = EntryZone(low=2042.0, high=2042.0)
    assert z.low == z.high


def test_situation_report_with_trade_proposal_round_trips():
    sr = SituationReport(
        timestamp="2026-05-02T18:14:00Z",
        instrument="XAUUSD",
        data_quality="FRESH",
        regime="accumulation",
        evidence=["a", "b", "c"],
        trapped_party=TrappedParty(who="late longs", level=2055.0, pain_bp=38.0),
        dominant_party=None,
        asymmetry="late longs no exit",
        thesis="thesis",
        trade_proposal=TradeProposal(
            decision="TAKE",
            direction="long",
            entry_zone=EntryZone(low=2041.5, high=2042.0),
            invalidation="below 2040",
            first_target=2050.0,
            rr_minimum=3.0,
            size_pct=2.0,
        ),
        kill_thesis="If M5 closes below 2040 with positive CVD, exit at market.",
        confidence=0.72,
        confidence_breakdown=ConfidenceBreakdown(flow=0.20, structure=0.18, context=0.18, intent=0.16),
        notes_to_future_atlas="watch NY open",
    )
    dumped = sr.model_dump()
    reloaded = SituationReport.model_validate(dumped)
    assert reloaded.trade_proposal.entry_zone.low == 2041.5
