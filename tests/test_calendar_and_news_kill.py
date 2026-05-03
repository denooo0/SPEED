"""Tests for the calendar feed and the gate's news-kill enforcement."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.data_layer.calendar_feed import CalendarConfig, CalendarFeed, NewsEvent
from src.signals.gate import CycleGate, GateConfig


@pytest.fixture
def calendar_with_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "events.json"
        events = [
            {"name": "NFP", "timestamp": 1_750_000_000_000, "impact": "high"},
            {"name": "CPI", "timestamp": 1_750_010_000_000, "impact": "high"},
            {"name": "Random Speech", "timestamp": 1_750_020_000_000, "impact": "low"},
        ]
        path.write_text(json.dumps(events))
        yield CalendarFeed(CalendarConfig(events_path=str(path)))


def test_calendar_loads_events(calendar_with_events):
    assert len(calendar_with_events.events) == 3
    # Sorted by timestamp
    assert calendar_with_events.events[0].name == "NFP"


def test_in_news_window_triggers_inside(calendar_with_events):
    # Exactly at the event time
    in_window, event = calendar_with_events.is_in_news_window(now_ms=1_750_000_000_000)
    assert in_window is True
    assert event.name == "NFP"


def test_in_news_window_triggers_within_lead(calendar_with_events):
    # 5 minutes before NFP
    in_window, event = calendar_with_events.is_in_news_window(
        now_ms=1_750_000_000_000 - 5 * 60_000
    )
    assert in_window is True


def test_outside_window(calendar_with_events):
    # 30 minutes before NFP — outside the 15-min lead
    in_window, _ = calendar_with_events.is_in_news_window(
        now_ms=1_750_000_000_000 - 30 * 60_000
    )
    assert in_window is False


def test_low_impact_does_not_block(calendar_with_events):
    # The "Random Speech" event is low-impact; default impact_filter is high only
    in_window, _ = calendar_with_events.is_in_news_window(now_ms=1_750_020_000_000)
    assert in_window is False


def test_missing_calendar_file_is_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        feed = CalendarFeed(CalendarConfig(events_path=str(Path(tmpdir) / "missing.json")))
        assert feed.events == []
        in_window, _ = feed.is_in_news_window()
        assert in_window is False


def test_gate_blocks_during_news_window(calendar_with_events):
    """Law 12 — news kill takes precedence over all other triggers."""
    gate = CycleGate(
        GateConfig(cooldown_seconds=0),
        calendar=calendar_with_events,
    )
    pack = {
        "now_ms": 1_750_000_000_000,
        "lens_flow": {"m5": {"volume_spike": True}},  # would normally fire
        "lens_structure": {"m5": {"last_bos": "bull"}},
        "lens_context": {"session": "ny-overlap"},
    }
    invoke, reason = gate.should_invoke(pack)
    assert invoke is False
    assert reason.startswith("news_kill:NFP")


def test_gate_unblocks_outside_window(calendar_with_events):
    gate = CycleGate(
        GateConfig(cooldown_seconds=0),
        calendar=calendar_with_events,
    )
    pack = {
        "now_ms": 1_750_000_000_000 - 60 * 60_000,  # one hour before NFP
        "lens_flow": {"m5": {"volume_spike": True}},
        "lens_structure": {"m5": {"last_bos": None}},
        "lens_context": {"session": "off-hours"},
    }
    invoke, reason = gate.should_invoke(pack)
    assert invoke is True
    assert "volume_spike" in reason


def test_gate_without_calendar_skips_news_check():
    """Backward compat: no calendar = no news enforcement."""
    gate = CycleGate(GateConfig(cooldown_seconds=0))  # calendar=None
    pack = {
        "now_ms": 1_750_000_000_000,
        "lens_flow": {"m5": {"volume_spike": True}},
        "lens_structure": {"m5": {"last_bos": None}},
        "lens_context": {"session": "off-hours"},
    }
    invoke, _ = gate.should_invoke(pack)
    assert invoke is True
