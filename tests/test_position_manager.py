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


# -- short-side semantics -------------------------------------------------
def _short_signal():
    """Short setup: entry 2000, SL 2005 (above), TPs below entry."""
    return {
        "entry_price": 2000.0,
        "stop_loss": 2005.0,
        "tp1": 1997.0,
        "tp2": 1994.0,
        "tp3": 1988.0,
        "position_size": 1.0,
        "reason": "short test",
        "direction": "short",
    }


def test_short_position_persists_direction(db_manager):
    pm = PositionManager(db_manager)
    record = pm.open_from_signal(_short_signal())
    fetched = db_manager.get_position(record["position_id"])
    assert fetched["direction"] == "short"


def test_short_sl_fires_on_price_above(db_manager):
    pm = PositionManager(db_manager)
    record = pm.open_from_signal(_short_signal())
    # Price moves up to SL — short loses
    actions = pm.evaluate(record, current_price=2005.5)
    assert len(actions) == 1
    assert actions[0].kind == "SL"
    # Short loses when price rises above entry
    assert actions[0].pnl < 0


def test_short_does_not_fake_hit_tp_on_open(db_manager):
    """Pre-fix bug: shorts triggered TP1 instantly because comparison was long-only."""
    pm = PositionManager(db_manager)
    record = pm.open_from_signal(_short_signal())
    # Price unchanged from entry — no TPs should fire
    actions = pm.evaluate(record, current_price=2000.0)
    assert actions == []


def test_short_tps_fire_on_price_below(db_manager):
    pm = PositionManager(db_manager)
    record = pm.open_from_signal(_short_signal())
    # Price drops past all three TPs in one shot
    actions = pm.evaluate(record, current_price=1985.0)
    kinds = [a.kind for a in actions]
    assert kinds == ["TP1", "TP2", "TP3"]
    # Short profits as price falls
    for a in actions:
        assert a.pnl > 0


def test_long_still_works_after_direction_refactor(db_manager):
    pm = PositionManager(db_manager)
    record = pm.open_from_signal(_make_signal())  # long
    # Price drops to SL
    actions = pm.evaluate(record, current_price=1990.0)
    assert actions[0].kind == "SL"
    assert actions[0].pnl < 0


def test_get_trades_for_position_filters_correctly(db_manager):
    pm = PositionManager(db_manager)
    rec_a = pm.open_from_signal(_make_signal())
    rec_b = pm.open_from_signal(_make_signal())
    pm.evaluate(rec_a, current_price=1990.0)  # close A on SL
    a_events = db_manager.get_trades_for_position(rec_a["position_id"])
    b_events = db_manager.get_trades_for_position(rec_b["position_id"])
    assert all(e["position_id"] == rec_a["position_id"] for e in a_events)
    assert all(e["position_id"] == rec_b["position_id"] for e in b_events)
    # B is still open with only the ENTRY event
    assert len(b_events) == 1


def test_open_from_signal_rejects_unsupported_direction(db_manager):
    import pytest as _pt
    pm = PositionManager(db_manager)
    bad = _make_signal()
    bad["direction"] = "sideways"
    with _pt.raises(ValueError):
        pm.open_from_signal(bad)
