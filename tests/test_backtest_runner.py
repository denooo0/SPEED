"""End-to-end runner test using a trivial always-long-on-first-bar-of-day strategy."""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import pytest

from src.backtest.cost_model import CostModel
from src.backtest.runner import BacktestRunner
from src.backtest.strategy import Bar, Order, PositionState


def _synthetic_candles(n_days: int = 5, bars_per_day: int = 288) -> pd.DataFrame:
    n = n_days * bars_per_day
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    rng = np.random.default_rng(7)
    closes = 100.0 * np.cumprod(1.0 + rng.normal(0.00005, 0.0005, n))
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.001,
            "low": np.minimum(opens, closes) * 0.999,
            "close": closes,
            "volume": np.full(n, 50.0),
        },
        index=idx,
    )


class AlwaysLongFirstBarOfDay:
    """Opens a long on the FIRST bar of each UTC day; closes via SL/TP or EOD."""

    def __init__(self, hypothesis: Optional[Dict[str, Any]] = None) -> None:
        self._last_open_day: Optional[pd.Timestamp] = None
        # Allow hypothesis to tune sl/tp distances in pips.
        h = hypothesis or {}
        self.sl_pips = float(h.get("sl_pips", 50))
        self.tp_pips = float(h.get("tp_pips", 100))

    def on_bar(self, bar: Bar, feature_pack: Dict[str, Any], position_state: PositionState) -> Optional[Order]:
        day = bar.timestamp.normalize()
        is_first_bar_today = (bar.timestamp - day).total_seconds() < 5 * 60
        if position_state.is_open:
            return None
        if not is_first_bar_today:
            return None
        if self._last_open_day is not None and self._last_open_day == day:
            return None
        self._last_open_day = day
        sl = bar.close * (1 - self.sl_pips / 10_000.0)
        tp = bar.close * (1 + self.tp_pips / 10_000.0)
        return Order(direction="long", entry_price=bar.close, stop_loss=sl, take_profits=[tp], size=1.0)


def test_runner_loads_dict_hypothesis_and_returns_verdict_shape():
    candles = _synthetic_candles(n_days=10)
    hypothesis = {"id": "H-TEST-001", "sl_pips": 30, "tp_pips": 60}

    runner = BacktestRunner(
        strategy_factory=lambda h: AlwaysLongFirstBarOfDay(h),
        cost_model=CostModel(),
        bar_seconds=300,
        walk_forward_geometry=(500, 200, 200),
        cpcv_params={"n_groups": 4, "k_test": 2, "embargo_pct": 0.0, "label_horizon_bars": 5},
    )
    result = runner.evaluate_hypothesis(
        hypothesis_yaml=hypothesis,
        candles=candles,
        feature_pack_provider=lambda _ts: {},
    )
    # Shape contract.
    for key in (
        "hypothesis_id",
        "in_sample",
        "walk_forward",
        "cpcv",
        "pbo",
        "dsr",
        "aggregated_oos_trades",
        "verdict",
        "fail_reasons",
        "gate",
    ):
        assert key in result, f"missing key: {key}"
    assert result["hypothesis_id"] == "H-TEST-001"
    assert result["verdict"] in ("PASS", "FAIL")


def test_runner_loads_from_yaml_path(tmp_path):
    pytest.importorskip("yaml")
    candles = _synthetic_candles(n_days=5)
    yaml_path = tmp_path / "h.yml"
    yaml_path.write_text("id: H-2026-0001\nsl_pips: 30\ntp_pips: 60\n")
    runner = BacktestRunner(
        strategy_factory=lambda h: AlwaysLongFirstBarOfDay(h),
        walk_forward_geometry=(300, 100, 100),
        cpcv_params={"n_groups": 4, "k_test": 2, "embargo_pct": 0.0, "label_horizon_bars": 5},
    )
    result = runner.evaluate_hypothesis(
        hypothesis_yaml=yaml_path,
        candles=candles,
        feature_pack_provider=lambda _ts: {},
    )
    assert result["hypothesis_id"] == "H-2026-0001"


def test_runner_fail_reasons_when_low_trade_count():
    """Tiny dataset → not enough OOS trades → must FAIL with the expected reason code."""
    candles = _synthetic_candles(n_days=2)  # 576 bars
    runner = BacktestRunner(
        strategy_factory=lambda h: AlwaysLongFirstBarOfDay(h),
        walk_forward_geometry=(300, 100, 100),
        cpcv_params={"n_groups": 4, "k_test": 2, "embargo_pct": 0.0, "label_horizon_bars": 5},
    )
    result = runner.evaluate_hypothesis(
        hypothesis_yaml={"id": "H-LOW", "sl_pips": 30, "tp_pips": 60},
        candles=candles,
        feature_pack_provider=lambda _ts: {},
    )
    assert result["verdict"] == "FAIL"
    assert any("aggregated OOS trades" in r for r in result["fail_reasons"])


def test_runner_end_to_end_metrics_populated():
    candles = _synthetic_candles(n_days=20)
    runner = BacktestRunner(
        strategy_factory=lambda h: AlwaysLongFirstBarOfDay(h),
        walk_forward_geometry=(1000, 300, 300),
        cpcv_params={"n_groups": 5, "k_test": 2, "embargo_pct": 0.0, "label_horizon_bars": 5},
    )
    result = runner.evaluate_hypothesis(
        hypothesis_yaml={"id": "H-E2E", "sl_pips": 30, "tp_pips": 60},
        candles=candles,
        feature_pack_provider=lambda _ts: {},
    )
    # Walk-forward should have run at least one step.
    assert result["walk_forward"]["n_steps"] >= 1
    # CPCV should have generated C(5,2) = 10 paths.
    assert result["cpcv"]["n_paths"] == 10
    assert 0.0 <= result["pbo"] <= 1.0
    assert 0.0 <= result["dsr"] <= 1.0
    # In-sample summary populated.
    assert "sharpe" in result["in_sample"]
    assert result["in_sample"]["trades"] > 0
