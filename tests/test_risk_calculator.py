"""Tests for risk/position sizing."""
from __future__ import annotations

import pytest

from src.risk.risk_calculator import RiskCalculator, pips_to_price, price_distance_pips


def test_pips_to_price_basis_points():
    # 25 "pips" (basis points) on price 2000 = 5.0
    assert pips_to_price(2000, 25) == 5.0


def test_price_distance_pips():
    assert abs(price_distance_pips(2005, 2000) - 25.0) < 1e-6


def test_risk_calculator_caps_size():
    rc = RiskCalculator(
        account_risk_pct=2.0,
        sl_distance_pips=25,
        tp_distances_pips=[15, 30, 60],
        max_units_per_trade=5.0,
    )
    p = rc.build(entry=2000.0, account_balance=100_000.0)
    assert p.position_size == 5.0
    assert p.stop_loss < p.entry
    assert p.tp1 > p.entry
    assert p.tp2 > p.tp1
    assert p.tp3 > p.tp2


def test_risk_calculator_pnl_consistent():
    rc = RiskCalculator(
        account_risk_pct=1.0,
        sl_distance_pips=50,
        tp_distances_pips=[50, 100, 150],
    )
    balance = 10_000.0
    p = rc.build(entry=2000.0, account_balance=balance)
    # Risk per trade = 1% of 10k = $100
    # SL at 50bp on 2000 = 10 price points away
    # 100 / 10 = 10 units
    assert abs(p.risk_amount - 100.0) < 1e-6
    assert abs(p.position_size - 10.0) < 1e-6


def test_risk_calculator_invalid_entry():
    rc = RiskCalculator(
        account_risk_pct=2.0,
        sl_distance_pips=25,
        tp_distances_pips=[15, 30, 60],
    )
    with pytest.raises(ValueError):
        rc.build(entry=0.0, account_balance=10_000.0)
