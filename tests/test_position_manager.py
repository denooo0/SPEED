"""Tests for position lifecycle and exit logic."""
from __future__ import annotations

import os
import tempfile

import pytest

from src.database.db_manager import DatabaseManager
from src.risk.position_manager import PositionManager


@pytest.fixture
def db_manager():
    tmpdir = tempfile.mkdtemp()
    db = DatabaseManager(db_dir=tmpdir, filename="test.db")
    yield db
    db.close()


def _make_signal():
    return {
        "entry_price": 2000.0,
        "stop_loss": 1995.0,
        "tp1": 2003.0,
        "tp2": 2006.0,
        "tp3": 2012.0,
        "position_size": 1.0,
        "reason": "test",
    }


def test_open_persists_position(db_manager):
    pm = PositionManager(db_manager, scale_out_pct=[25, 25, 25, 25])
    record = pm.open_from_signal(_make_signal())
    assert record["status"] == "OPEN"
    assert pm.has_open_position()
    fetched = db_manager.get_position(record["position_id"])
    assert fetched is not None
    assert fetched["entry_price"] == 2000.0
    assert fetched["tp_targets_remaining"] == ["TP1", "TP2", "TP3"]


def test_partial_scale_out_at_tp1(db_manager):
    pm = PositionManager(db_manager, scale_out_pct=[25, 25, 25, 25])
    record = pm.open_from_signal(_make_signal())
    actions = pm.evaluate(record, current_price=2003.5)
    assert len(actions) == 1
    assert actions[0].kind == "TP1"
    assert pytest.approx(actions[0].size) == 0.25
    refreshed = db_manager.get_position(record["position_id"])
    assert refreshed["status"] == "PARTIALLY_CLOSED"
    assert "TP1" not in refreshed["tp_targets_remaining"]


def test_stop_loss_closes_full_position(db_manager):
    pm = PositionManager(db_manager, scale_out_pct=[25, 25, 25, 25])
    record = pm.open_from_signal(_make_signal())
    actions = pm.evaluate(record, current_price=1990.0)
    assert len(actions) == 1
    assert actions[0].kind == "SL"
    refreshed = db_manager.get_position(record["position_id"])
    assert refreshed["status"] == "CLOSED"
    assert refreshed["tp_targets_remaining"] == []


def test_full_run_through_all_tps(db_manager):
    pm = PositionManager(db_manager, scale_out_pct=[25, 25, 25, 25])
    record = pm.open_from_signal(_make_signal())
    actions = pm.evaluate(record, current_price=2015.0)
    kinds = [a.kind for a in actions]
    assert kinds == ["TP1", "TP2", "TP3"]
    refreshed = db_manager.get_position(record["position_id"])
    assert refreshed["status"] == "CLOSED"


def test_resume_open_positions_after_restart(db_manager):
    pm = PositionManager(db_manager)
    pm.open_from_signal(_make_signal())
    pm.open_from_signal(_make_signal())
    # Simulate restart with a fresh manager but same db file
    pm2 = PositionManager(db_manager)
    assert len(pm2.get_open_positions()) == 2
