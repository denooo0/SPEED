"""Walk-forward runner tests."""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import pytest

from src.backtest.cost_model import CostModel
from src.backtest.simulator import EventDrivenSimulator
from src.backtest.strategy import Bar, Order, PositionState
from src.backtest.walk_forward import WalkForwardRunner


def _candles(n: int, slope_bp_per_bar: float = 5.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    closes = 100.0 * np.cumprod(1.0 + np.full(n, slope_bp_per_bar / 10_000.0))
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    df = pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.0005,
            "low": np.minimum(opens, closes) * 0.9995,
            "close": closes,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )
    return df


class _LongAndHold:
    """Open a long on the first flat bar; never close (relies on EOD)."""

    def on_bar(self, bar: Bar, fp: Dict[str, Any], ps: PositionState) -> Optional[Order]:
        if ps.is_open:
            return None
        return Order(
            direction="long",
            entry_price=bar.close,
            stop_loss=bar.close * 0.1,
            take_profits=[bar.close * 10.0],
            size=1.0,
        )


def test_walk_forward_validates_window_sizes():
    df = _candles(200)
    sim = EventDrivenSimulator(df, CostModel())
    with pytest.raises(ValueError):
        WalkForwardRunner(sim, is_window_bars=0, oos_window_bars=10, step_bars=5)
    with pytest.raises(ValueError):
        WalkForwardRunner(sim, is_window_bars=10, oos_window_bars=0, step_bars=5)


def test_walk_forward_step_count_matches_geometry():
    n = 100
    df = _candles(n)
    sim = EventDrivenSimulator(df, CostModel(slippage_bps_normal=0, fee_bps_taker=0, spread_bps_normal=0))
    wf = WalkForwardRunner(sim, is_window_bars=40, oos_window_bars=20, step_bars=20)
    result = wf.run(_LongAndHold(), feature_pack_provider=lambda _ts: {})
    # n=100, IS=40, OOS=20: first window starts at 0, ends at 59. Step=20.
    # Starts: 0, 20, 40. At start=40 → window covers 40..99, fits. start=60 → 60..119 > 99, stop.
    assert result.n_steps == 3
    assert all("wfe" in s for s in result.per_step)


def test_walk_forward_wfe_passes_in_trending_market():
    """A perpetually-rising series with long-and-hold should pass WFE for every step.

    Use equal IS/OOS window sizes so a perfectly-stationary uptrend yields WFE ~ 1.
    """
    n = 200
    df = _candles(n, slope_bp_per_bar=10.0)
    sim = EventDrivenSimulator(df, CostModel(slippage_bps_normal=0, fee_bps_taker=0, spread_bps_normal=0))
    wf = WalkForwardRunner(sim, is_window_bars=40, oos_window_bars=40, step_bars=40)
    result = wf.run(_LongAndHold(), feature_pack_provider=lambda _ts: {})
    assert result.n_steps >= 2
    assert result.wfe_pass_ratio >= 0.7
    for s in result.per_step:
        assert s["is_return"] > 0
        assert s["oos_return"] > 0


def test_walk_forward_wfe_fails_in_collapsing_oos():
    """IS positive, OOS negative → every step fails."""
    n = 100
    # Trending up then suddenly down.
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    closes = np.concatenate([np.linspace(100, 120, 60), np.linspace(120, 80, 40)])
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    df = pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.0005,
            "low": np.minimum(opens, closes) * 0.9995,
            "close": closes,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )
    sim = EventDrivenSimulator(df, CostModel(slippage_bps_normal=0, fee_bps_taker=0, spread_bps_normal=0))
    wf = WalkForwardRunner(sim, is_window_bars=50, oos_window_bars=30, step_bars=20)
    result = wf.run(_LongAndHold(), feature_pack_provider=lambda _ts: {})
    # At least the step where IS is up + OOS is the crash should fail.
    assert any(s["wfe"] == 0.0 for s in result.per_step)


def test_walk_forward_wfe_zero_when_is_negative():
    """If IS lost money, WFE returns 0 (negative IS doesn't qualify as edge)."""
    n = 100
    df = _candles(n, slope_bp_per_bar=-5.0)
    sim = EventDrivenSimulator(df, CostModel(slippage_bps_normal=0, fee_bps_taker=0, spread_bps_normal=0))
    wf = WalkForwardRunner(sim, is_window_bars=40, oos_window_bars=20, step_bars=40)
    result = wf.run(_LongAndHold(), feature_pack_provider=lambda _ts: {})
    for s in result.per_step:
        assert s["is_return"] <= 0
        assert s["wfe"] == 0.0
    assert result.wfe_pass_ratio == 0.0
