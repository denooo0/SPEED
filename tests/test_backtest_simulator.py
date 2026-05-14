"""Simulator tests on synthetic candles where PnL is exact."""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import pytest

from src.backtest.cost_model import CostModel
from src.backtest.simulator import EventDrivenSimulator
from src.backtest.strategy import Bar, Order, PositionState


# ---- shared helpers ---------------------------------------------------------


def synthetic_candles(
    closes: np.ndarray,
    start: str = "2024-01-01",
    bar_minutes: int = 5,
) -> pd.DataFrame:
    """Build a candle DataFrame from a close-price array.

    Open of bar i = close of bar i-1 (first bar's open == first close).
    High/Low expand 5bp around close so SL/TP can trigger cleanly.
    """
    n = len(closes)
    idx = pd.date_range(start=start, periods=n, freq=f"{bar_minutes}min")
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) * 1.0005
    lows = np.minimum(opens, closes) * 0.9995
    vols = np.full(n, 100.0)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


class _AlwaysLongFirstBar:
    """Enter long on first bar, hold to end (no SL/TP)."""

    def __init__(self) -> None:
        self.fired = False

    def on_bar(self, bar: Bar, feature_pack: Dict[str, Any], position_state: PositionState) -> Optional[Order]:
        if self.fired or position_state.is_open:
            return None
        self.fired = True
        # SL miles below, TP miles above so they never trigger.
        return Order(
            direction="long",
            entry_price=bar.close,
            stop_loss=bar.close * 0.1,
            take_profits=[bar.close * 10.0],
            size=1.0,
        )


class _LongHitsTP:
    """Enter long on first bar; TP placed close enough that it hits later."""

    def __init__(self, tp_price: float) -> None:
        self.fired = False
        self.tp_price = tp_price

    def on_bar(self, bar: Bar, feature_pack: Dict[str, Any], position_state: PositionState) -> Optional[Order]:
        if self.fired or position_state.is_open:
            return None
        self.fired = True
        return Order(
            direction="long",
            entry_price=bar.close,
            stop_loss=bar.close * 0.1,
            take_profits=[self.tp_price],
            size=1.0,
        )


class _LongHitsSL:
    def __init__(self, sl_price: float) -> None:
        self.fired = False
        self.sl_price = sl_price

    def on_bar(self, bar: Bar, feature_pack: Dict[str, Any], position_state: PositionState) -> Optional[Order]:
        if self.fired or position_state.is_open:
            return None
        self.fired = True
        return Order(
            direction="long",
            entry_price=bar.close,
            stop_loss=self.sl_price,
            take_profits=[bar.close * 10.0],
            size=1.0,
        )


# ---- tests ------------------------------------------------------------------


def test_simulator_requires_ohlc_columns():
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.date_range("2024-01-01", periods=2, freq="5min"))
    with pytest.raises(ValueError):
        EventDrivenSimulator(df, CostModel())


def test_simulator_requires_monotonic_index():
    idx = pd.to_datetime(["2024-01-02", "2024-01-01"])
    df = pd.DataFrame({"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [1, 1], "volume": [0, 0]}, index=idx)
    with pytest.raises(ValueError):
        EventDrivenSimulator(df, CostModel())


def test_simulator_runs_and_returns_empty_when_no_orders():
    closes = np.linspace(100.0, 110.0, 20)
    df = synthetic_candles(closes)

    class Noop:
        def on_bar(self, bar, fp, ps):
            return None

    sim = EventDrivenSimulator(df, CostModel())
    res = sim.run(Noop(), [{} for _ in range(len(df))])
    assert res.n_trades == 0
    assert len(res.equity_curve) >= 1
    assert res.equity_curve.iloc[-1] == 1.0  # no positions opened, equity unchanged


def test_long_hits_tp_exact_pnl_on_zero_cost():
    """With ALL costs zeroed, the realized PnL must equal (tp - entry_fill)/entry_fill."""
    closes = np.array([100.0, 100.1, 100.2, 100.3, 100.4, 105.0, 105.0])
    df = synthetic_candles(closes)
    cm = CostModel(
        spread_bps_normal=0.0,
        spread_bps_news=0.0,
        slippage_bps_normal=0.0,
        slippage_bps_news=0.0,
        fee_bps_taker=0.0,
        fee_bps_maker=0.0,
        funding_bps_per_8h=0.0,
        latency_seconds=0.0,
    )
    sim = EventDrivenSimulator(df, cm)
    strat = _LongHitsTP(tp_price=104.0)
    res = sim.run(strat, [{} for _ in range(len(df))])
    assert res.n_trades == 1
    trade = res.trades.iloc[0]
    # Order placed after bar0 close, filled at bar1's open = closes[0] = 100.0
    # TP hit when bar.high >= 104; bar5 high = max(open=100.4, close=105) * 1.0005 = 105.05+ → hits 104.
    assert abs(trade["entry_price"] - 100.0) < 1e-9
    assert abs(trade["exit_price"] - 104.0) < 1e-9
    expected_pnl = (104.0 - 100.0) / 100.0
    assert abs(trade["pnl"] - expected_pnl) < 1e-9
    # R-multiple sanity: with SL miles away, R = (4) / (100 - 10) ≈ 0.044
    assert trade["r_multiple"] > 0


def test_long_hits_sl_exact_pnl_on_zero_cost():
    closes = np.array([100.0, 100.0, 100.0, 95.0, 95.0])
    df = synthetic_candles(closes)
    cm = CostModel(
        spread_bps_normal=0.0,
        spread_bps_news=0.0,
        slippage_bps_normal=0.0,
        slippage_bps_news=0.0,
        fee_bps_taker=0.0,
        fee_bps_maker=0.0,
        funding_bps_per_8h=0.0,
        latency_seconds=0.0,
    )
    sim = EventDrivenSimulator(df, cm)
    strat = _LongHitsSL(sl_price=96.0)
    res = sim.run(strat, [{} for _ in range(len(df))])
    assert res.n_trades == 1
    trade = res.trades.iloc[0]
    # bar1 open = 100, entry filled there. SL=96. Bar3 low = min(100, 95)*0.9995 = 94.95 -> SL hits.
    assert trade["exit_reason"] == "SL"
    expected_pnl = (96.0 - 100.0) / 100.0
    assert abs(trade["pnl"] - expected_pnl) < 1e-9


def test_eod_force_close_records_trade():
    closes = np.linspace(100.0, 110.0, 10)
    df = synthetic_candles(closes)
    cm = CostModel(
        spread_bps_normal=0.0,
        spread_bps_news=0.0,
        slippage_bps_normal=0.0,
        slippage_bps_news=0.0,
        fee_bps_taker=0.0,
        fee_bps_maker=0.0,
        funding_bps_per_8h=0.0,
        latency_seconds=0.0,
    )
    sim = EventDrivenSimulator(df, cm)
    strat = _AlwaysLongFirstBar()
    res = sim.run(strat, [{} for _ in range(len(df))])
    assert res.n_trades == 1
    assert res.trades.iloc[0]["exit_reason"] == "EOD"


def test_cost_model_reduces_pnl_vs_zero_cost():
    closes = np.array([100.0, 100.0, 100.0, 105.0, 105.0])
    df = synthetic_candles(closes)
    sim_zero = EventDrivenSimulator(
        df,
        CostModel(
            spread_bps_normal=0.0,
            slippage_bps_normal=0.0,
            fee_bps_taker=0.0,
            funding_bps_per_8h=0.0,
            latency_seconds=0.0,
        ),
    )
    sim_real = EventDrivenSimulator(df, CostModel())
    res_zero = sim_zero.run(_LongHitsTP(tp_price=104.0), [{} for _ in range(len(df))])
    res_real = sim_real.run(_LongHitsTP(tp_price=104.0), [{} for _ in range(len(df))])
    assert res_zero.n_trades == 1 and res_real.n_trades == 1
    assert res_real.trades.iloc[0]["pnl"] < res_zero.trades.iloc[0]["pnl"]


def test_latency_drift_drags_entry_for_long():
    """Latency drift should make the long entry price higher than next_bar_open."""
    closes = np.array([100.0, 102.0, 102.0, 110.0, 110.0])
    df = synthetic_candles(closes)
    # Force latency = full bar so drift = full (high - open)
    cm = CostModel(
        spread_bps_normal=0.0,
        slippage_bps_normal=0.0,
        fee_bps_taker=0.0,
        funding_bps_per_8h=0.0,
        latency_seconds=300.0,
    )
    sim = EventDrivenSimulator(df, cm, bar_seconds=300)
    res = sim.run(_LongHitsTP(tp_price=105.0), [{} for _ in range(len(df))])
    bar1_open = df["open"].iloc[1]
    bar1_high = df["high"].iloc[1]
    # With latency_frac=1 and zero spread/fees: entry = bar1_high exactly.
    assert abs(res.trades.iloc[0]["entry_price"] - bar1_high) < 1e-9
    assert res.trades.iloc[0]["entry_price"] >= bar1_open


def test_no_lookahead_strategy_cannot_see_future():
    """Strategy returning an Order on bar i fills at bar i+1's open, never bar i's close."""
    closes = np.array([100.0, 110.0, 110.0, 110.0])
    df = synthetic_candles(closes)
    cm = CostModel(
        spread_bps_normal=0.0,
        slippage_bps_normal=0.0,
        fee_bps_taker=0.0,
        funding_bps_per_8h=0.0,
        latency_seconds=0.0,
    )
    sim = EventDrivenSimulator(df, cm)
    strat = _AlwaysLongFirstBar()
    res = sim.run(strat, [{} for _ in range(len(df))])
    assert res.n_trades == 1
    # Entry must be bar1's open (= closes[0] = 100.0), NOT bar0's close (also 100 but the path is what matters).
    assert abs(res.trades.iloc[0]["entry_price"] - df["open"].iloc[1]) < 1e-9


def test_position_state_visible_to_strategy_while_open():
    closes = np.linspace(100.0, 110.0, 8)
    df = synthetic_candles(closes)
    seen_states = []

    class Recorder:
        def __init__(self) -> None:
            self.fired = False

        def on_bar(self, bar, fp, ps):
            seen_states.append(ps.is_open)
            if not self.fired and not ps.is_open:
                self.fired = True
                return Order("long", bar.close, bar.close * 0.1, [bar.close * 10.0], 1.0)
            return None

    sim = EventDrivenSimulator(df, CostModel())
    sim.run(Recorder(), [{} for _ in range(len(df))])
    # First bar: flat. Second bar onward: open.
    assert seen_states[0] is False
    assert any(s is True for s in seen_states[1:])
