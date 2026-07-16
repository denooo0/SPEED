"""Tests for the discretionary Trade Copilot."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.copilot import discipline_gate as gate
from src.copilot.copilot import TradeCopilot
from src.copilot.journal import TradeJournal
from src.copilot.position_sizer import (
    SizerConfig,
    full_kelly_fraction,
    recommend_risk_pct,
    size_from_risk,
)
from src.copilot.schema import SetupStats, TradeProposal


def _good_short(**over):
    base = dict(
        instrument="XAUUSD", direction="short", entry=4128.0, stop=4140.0, target=4090.0,
        setup="htf-retracement-short",
        thesis="H1 downtrend, sold retracement into declining MA; late longs trapped above",
        kill_thesis="H1 closes back above 4140 with positive momentum for two bars",
        conviction=4, session="ny",
    )
    base.update(over)
    return TradeProposal(**base)


@pytest.fixture
def journal():
    with tempfile.TemporaryDirectory() as tmp:
        yield TradeJournal(root=Path(tmp))


# -- schema ---------------------------------------------------------------
def test_proposal_rr_and_risk():
    p = _good_short()
    assert p.risk_per_unit == pytest.approx(12.0)
    assert p.rr == pytest.approx((4128 - 4090) / 12.0)


# -- discipline gate ------------------------------------------------------
def test_gate_approves_clean_trade():
    checks = gate.evaluate(_good_short(), SetupStats(setup="x"))
    assert gate.decide(checks) in ("APPROVE", "APPROVE_WITH_WARNINGS")
    assert not any(c.severity == "block" and not c.passed for c in checks)


def test_gate_blocks_low_rr():
    p = _good_short(target=4120.0)  # tiny reward, R:R < 2
    checks = gate.evaluate(p, SetupStats(setup="x"))
    assert gate.decide(checks) == "BLOCK"
    assert any(c.name == "risk_reward" and not c.passed for c in checks)


def test_gate_blocks_vague_kill_thesis():
    p = _good_short(kill_thesis="if it goes against me")
    checks = gate.evaluate(p, SetupStats(setup="x"))
    assert any(c.name == "kill_thesis" and not c.passed for c in checks)
    assert gate.decide(checks) == "BLOCK"


def test_gate_blocks_wrong_side_stop():
    p = _good_short(stop=4100.0)  # stop below entry on a short = wrong side
    checks = gate.evaluate(p, SetupStats(setup="x"))
    assert any(c.name == "stop_side" and not c.passed for c in checks)


def test_gate_blocks_second_position():
    checks = gate.evaluate(_good_short(), SetupStats(setup="x"), open_positions=1)
    assert any(c.name == "one_position" and not c.passed for c in checks)
    assert gate.decide(checks) == "BLOCK"


def test_gate_blocks_daily_loss_cap():
    checks = gate.evaluate(_good_short(), SetupStats(setup="x"), day_pnl_pct=-5.0)
    assert any(c.name == "daily_loss_cap" and not c.passed for c in checks)


def test_gate_blocks_drawdown_halt():
    checks = gate.evaluate(_good_short(), SetupStats(setup="x"), account_dd_pct=22.0)
    assert any(c.name == "drawdown_halt" and not c.passed for c in checks)


def test_gate_blocks_news_window():
    checks = gate.evaluate(_good_short(), SetupStats(setup="x"), in_news_window=True)
    assert any(c.name == "news_window" and not c.passed for c in checks)


def test_gate_warns_on_losing_setup():
    losing = SetupStats(setup="bad", n=10, wins=3, losses=7, sum_r=-2.0, sum_win_r=6.0, sum_loss_r=-8.0)
    checks = gate.evaluate(_good_short(setup="bad"), losing)
    warn = [c for c in checks if c.name == "setup_track_record"]
    assert warn and not warn[0].passed and warn[0].severity == "warn"


# -- position sizer -------------------------------------------------------
def test_kelly_zero_when_no_edge():
    s = SetupStats(setup="x", n=30, wins=10, losses=20, sum_win_r=10.0, sum_loss_r=-20.0)
    assert full_kelly_fraction(s) == 0.0


def test_kelly_positive_with_edge():
    # 55% win, avg win +2R, avg loss -1R
    s = SetupStats(setup="x", n=30, wins=17, losses=13, sum_win_r=34.0, sum_loss_r=-13.0)
    f = full_kelly_fraction(s)
    assert f > 0


def test_sizer_conservative_before_enough_data():
    s = SetupStats(setup="x", n=5, wins=3, losses=2, sum_win_r=6.0, sum_loss_r=-2.0)
    risk, why = recommend_risk_pct(s, conviction=3)
    assert risk <= 0.6  # default-ish
    assert "conservative default" in why.lower() or "need" in why.lower()


def test_sizer_desizes_losing_setup_after_data():
    s = SetupStats(setup="x", n=30, wins=9, losses=21, sum_win_r=18.0, sum_loss_r=-21.0)
    risk, why = recommend_risk_pct(s, conviction=5)
    assert risk <= 0.5  # floored — Kelly says don't bet
    assert "don't bet" in why.lower() or "non-positive" in why.lower()


def test_sizer_caps_at_max():
    # huge edge would blow past cap; must clamp
    s = SetupStats(setup="x", n=50, wins=45, losses=5, sum_win_r=135.0, sum_loss_r=-5.0)
    risk, _ = recommend_risk_pct(s, conviction=5, config=SizerConfig(max_risk_pct=2.0))
    assert risk <= 2.0


def test_size_from_risk_math():
    # risk 1% of 100k = 1000; risk-per-unit 10 → 100 units
    assert size_from_risk(1.0, 100_000, 10.0) == pytest.approx(100.0)


# -- copilot orchestrator + journal + learning ----------------------------
def test_copilot_review_and_commit(journal):
    cop = TradeCopilot(journal=journal)
    v = cop.review(_good_short(), account_equity=100_000)
    assert v.decision in ("APPROVE", "APPROVE_WITH_WARNINGS")
    assert v.recommended_size > 0
    rec = cop.commit(v)
    assert rec is not None and rec.status == "OPEN"
    assert len(journal.open_positions()) == 1


def test_copilot_refuses_to_commit_block(journal):
    cop = TradeCopilot(journal=journal)
    v = cop.review(_good_short(target=4125.0), account_equity=100_000)  # low R:R → block
    assert v.decision == "BLOCK"
    assert cop.commit(v) is None
    assert len(journal.open_positions()) == 0


def test_copilot_learns_from_closed_trades(journal):
    cop = TradeCopilot(journal=journal)
    # Log + close several winning shorts of the same setup.
    for i in range(3):
        v = cop.review(_good_short(), account_equity=100_000)
        rec = cop.commit(v)
        # winner: price fell to target (short profits)
        cop.close(rec.trade_id, exit_price=4090.0, exit_reason="TP")
    stats = journal.stats_for("htf-retracement-short")
    assert stats.n == 3
    assert stats.wins == 3
    assert stats.expectancy_r > 0


def test_second_position_blocked_after_open(journal):
    cop = TradeCopilot(journal=journal)
    v1 = cop.review(_good_short(), account_equity=100_000)
    cop.commit(v1)
    # Now a second proposal should be blocked (one position at a time).
    v2 = cop.review(_good_short(), account_equity=100_000)
    assert v2.decision == "BLOCK"
    assert any(c.name == "one_position" and not c.passed for c in v2.checks)
