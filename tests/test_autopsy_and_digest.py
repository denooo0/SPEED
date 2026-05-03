"""Tests for autopsy writer + digest builder."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.llm.schema import (
    ConfidenceBreakdown,
    EntryZone,
    SituationReport,
    TradeProposal,
)
from src.memory.autopsy_writer import AutopsyWriter
from src.memory.digest_builder import DigestBuilder
from src.memory.markdown_store import MarkdownMemory


@pytest.fixture
def memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield MarkdownMemory(root=Path(tmpdir))


def _example_sr(direction: str = "long") -> SituationReport:
    return SituationReport(
        timestamp="2026-05-02T18:14:00Z",
        instrument="XAUUSD",
        data_quality="FRESH",
        regime="accumulation",
        evidence=["test"],
        trapped_party=None,
        dominant_party=None,
        asymmetry="test",
        thesis="thesis text",
        trade_proposal=TradeProposal(
            decision="TAKE",
            direction=direction,
            entry_zone=EntryZone(low=2041.5, high=2042.0),
            invalidation="below 2040",
            first_target=2050.0,
            rr_minimum=3.0,
            size_pct=2.0,
        ),
        kill_thesis="below 2040 with CVD positive",
        confidence=0.72,
        confidence_breakdown=ConfidenceBreakdown(flow=0.2, structure=0.2, context=0.2, intent=0.12),
        notes_to_future_atlas="test note",
    )


def _make_position(entry: float = 2000.0, sl: float = 1995.0, size: float = 1.0) -> Dict[str, Any]:
    return {
        "position_id": "abcdef1234",
        "entry_price": entry,
        "entry_time": 1714670000000,
        "entry_size": size,
        "sl": sl,
        "tp1": 2003.0,
        "tp2": 2006.0,
        "tp3": 2012.0,
        "status": "CLOSED",
    }


def test_autopsy_writes_won_trade(memory):
    writer = AutopsyWriter(memory)
    position = _make_position()
    events = [
        {"event": "ENTRY", "price": 2000.0, "size": 1.0, "pnl": None, "rule_matched": None, "position_id": "abcdef1234"},
        {"event": "SCALE_OUT_TP1", "price": 2003.0, "size": 0.25, "pnl": 0.75, "rule_matched": "TP1", "position_id": "abcdef1234"},
        {"event": "SCALE_OUT_TP2", "price": 2006.0, "size": 0.25, "pnl": 1.5, "rule_matched": "TP2", "position_id": "abcdef1234"},
        {"event": "SCALE_OUT_TP3", "price": 2012.0, "size": 0.25, "pnl": 3.0, "rule_matched": "TP3", "position_id": "abcdef1234"},
    ]
    path = writer.from_closed_position(position, events, triggering_sr=_example_sr())
    assert path.exists()
    fm, body = memory.load_autopsy(path)
    assert fm["result"] == "won"
    assert fm["r_multiple"] > 0
    assert "kill-thesis-clean" not in fm["tags"]
    assert "regime:accumulation" in fm["tags"]
    assert "What the SITUATION REPORT predicted" in body


def test_autopsy_writes_lost_trade_with_kill_thesis_tag(memory):
    writer = AutopsyWriter(memory)
    position = _make_position()
    events = [
        {"event": "ENTRY", "price": 2000.0, "size": 1.0, "pnl": None, "rule_matched": None, "position_id": "abcdef1234"},
        {"event": "EXIT", "price": 1995.0, "size": 1.0, "pnl": -5.0, "rule_matched": "SL", "position_id": "abcdef1234"},
    ]
    path = writer.from_closed_position(position, events, triggering_sr=_example_sr())
    fm, _ = memory.load_autopsy(path)
    assert fm["result"] == "lost"
    assert fm["r_multiple"] < 0
    assert fm["kill_thesis_triggered"] is True
    assert "kill-thesis-clean" in fm["tags"]


def test_autopsy_handles_missing_sr(memory):
    writer = AutopsyWriter(memory)
    events = [
        {"event": "ENTRY", "price": 2000.0, "size": 1.0, "pnl": None, "rule_matched": None, "position_id": "abcdef1234"},
        {"event": "EXIT", "price": 1995.0, "size": 1.0, "pnl": -5.0, "rule_matched": "SL", "position_id": "abcdef1234"},
    ]
    path = writer.from_closed_position(_make_position(), events, triggering_sr=None)
    fm, body = memory.load_autopsy(path)
    assert fm["result"] == "lost"
    assert "What the SITUATION REPORT predicted" not in body


def test_digest_empty_when_no_autopsies(memory):
    builder = DigestBuilder(memory)
    digest = builder.rebuild()
    assert "Memory empty" in digest


def test_digest_aggregates_wins_and_losses(memory):
    writer = AutopsyWriter(memory)
    # 2 wins (distinct ids and entry_times so files don't collide)
    for i in range(2):
        pos = _make_position()
        pos["position_id"] = f"won-{i:08d}"
        pos["entry_time"] = 1714670000000 + i * 3_600_000
        writer.from_closed_position(
            position=pos,
            trade_events=[
                {"event": "EXIT", "price": 2010.0, "size": 1.0, "pnl": 10.0, "rule_matched": "TP3", "position_id": pos["position_id"]},
            ],
            triggering_sr=_example_sr(),
        )
    # 3 losses with same regime tag
    for i in range(3):
        pos = _make_position()
        pos["position_id"] = f"lost-{i:08d}"
        pos["entry_time"] = 1714680000000 + i * 3_600_000
        writer.from_closed_position(
            position=pos,
            trade_events=[
                {"event": "EXIT", "price": 1995.0, "size": 1.0, "pnl": -5.0, "rule_matched": "SL", "position_id": pos["position_id"]},
            ],
            triggering_sr=_example_sr(),
        )
    digest = DigestBuilder(memory).rebuild()
    assert "Wins: 2" in digest
    assert "Losses: 3" in digest
    assert "regime:accumulation" in digest
