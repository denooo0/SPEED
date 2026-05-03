"""Tests for the cycle gate."""
from __future__ import annotations

from src.signals.gate import CycleGate, GateConfig


def _pack(volume_spike=False, last_bos=None, session="off-hours"):
    return {
        "lens_flow": {"m5": {"volume_spike": volume_spike}},
        "lens_structure": {"m5": {"last_bos": last_bos}},
        "lens_context": {"session": session},
    }


def test_gate_skips_quiet_pack():
    gate = CycleGate(GateConfig(cooldown_seconds=0))
    invoke, reason = gate.should_invoke(_pack())
    assert invoke is False
    assert reason == "no_trigger"


def test_gate_fires_on_volume_spike():
    gate = CycleGate(GateConfig(cooldown_seconds=0))
    invoke, reason = gate.should_invoke(_pack(volume_spike=True))
    assert invoke is True
    assert "volume_spike" in reason


def test_gate_fires_on_bos():
    gate = CycleGate(GateConfig(cooldown_seconds=0))
    invoke, reason = gate.should_invoke(_pack(last_bos="bull"))
    assert invoke is True
    assert "bos_bull" in reason


def test_gate_fires_once_on_session_open():
    gate = CycleGate(GateConfig(cooldown_seconds=0))
    invoke, reason = gate.should_invoke(_pack(session="london"))
    assert invoke is True and "session_open_london" in reason
    # Same session again should not retrigger session_open
    invoke2, reason2 = gate.should_invoke(_pack(session="london"))
    assert invoke2 is False
    # Switching to ny-overlap counts as a new session-open
    invoke3, reason3 = gate.should_invoke(_pack(session="ny-overlap"))
    assert invoke3 is True and "ny-overlap" in reason3


def test_gate_respects_cooldown():
    gate = CycleGate(GateConfig(cooldown_seconds=600))
    invoke, _ = gate.should_invoke(_pack(volume_spike=True))
    assert invoke is True
    invoke2, reason = gate.should_invoke(_pack(volume_spike=True))
    assert invoke2 is False
    assert reason == "cooldown"


def test_gate_disable_individual_triggers():
    gate = CycleGate(
        GateConfig(invoke_on_volume_spike=False, invoke_on_bos=True, cooldown_seconds=0)
    )
    invoke, _ = gate.should_invoke(_pack(volume_spike=True))
    assert invoke is False
    invoke2, _ = gate.should_invoke(_pack(last_bos="bear"))
    assert invoke2 is True


def test_gate_does_not_track_session_when_disabled():
    """Regression: prior bug — _last_session updated even with trigger off,
    so flipping the flag on later swallowed the first transition."""
    gate = CycleGate(
        GateConfig(invoke_on_session_open=False, cooldown_seconds=0)
    )
    # Many cycles in london while session-open trigger is OFF
    for _ in range(3):
        invoke, _ = gate.should_invoke(_pack(session="london"))
        assert invoke is False

    # Operator flips the flag on
    gate.config.invoke_on_session_open = True
    # Next cycle in london should NOW fire — _last_session was never set
    invoke, reason = gate.should_invoke(_pack(session="london"))
    assert invoke is True
    assert "session_open_london" in reason
