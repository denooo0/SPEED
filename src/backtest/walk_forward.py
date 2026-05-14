"""Walk-forward validation runner.

Rolling walk-forward: for each step we run the strategy on an in-sample
window and an out-of-sample window immediately following it. Walk-forward
efficiency (WFE) per step = OOS_return / IS_return.

A step passes if `wfe >= wfe_threshold` (default 0.5). The overall WFE pass
ratio is the fraction of steps that pass — the gate the v9 plan requires to
be >= 0.7.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

from src.backtest.simulator import BacktestResult, EventDrivenSimulator
from src.backtest.strategy import Strategy


@dataclass
class WalkForwardResult:
    per_step: List[Dict[str, float]] = field(default_factory=list)
    wfe_pass_ratio: float = 0.0
    n_steps: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "per_step": self.per_step,
            "wfe_pass_ratio": self.wfe_pass_ratio,
            "n_steps": self.n_steps,
        }


class WalkForwardRunner:
    """Rolling walk-forward orchestrator.

    Parameters
    ----------
    simulator:
        EventDrivenSimulator. The full candle range lives on the simulator;
        we slice it window by window.
    is_window_bars:
        Length of the in-sample window in bars.
    oos_window_bars:
        Length of the out-of-sample window in bars (must be > 0).
    step_bars:
        Stride between successive windows in bars.
    wfe_threshold:
        Minimum acceptable WFE for a step to "pass".
    """

    def __init__(
        self,
        simulator: EventDrivenSimulator,
        is_window_bars: int,
        oos_window_bars: int,
        step_bars: int,
        wfe_threshold: float = 0.5,
    ) -> None:
        if is_window_bars <= 0 or oos_window_bars <= 0 or step_bars <= 0:
            raise ValueError("window/step sizes must be positive")
        self.simulator = simulator
        self.is_window_bars = int(is_window_bars)
        self.oos_window_bars = int(oos_window_bars)
        self.step_bars = int(step_bars)
        self.wfe_threshold = float(wfe_threshold)

    # ----- public API -----

    def run(
        self,
        strategy: Strategy,
        feature_pack_provider: Callable[[pd.Timestamp], Dict[str, Any]],
    ) -> WalkForwardResult:
        candles = self.simulator.candles
        index = candles.index
        n = len(index)
        result = WalkForwardResult()

        i = 0
        while i + self.is_window_bars + self.oos_window_bars <= n:
            is_start_idx = i
            is_end_idx = i + self.is_window_bars - 1
            oos_start_idx = i + self.is_window_bars
            oos_end_idx = oos_start_idx + self.oos_window_bars - 1

            is_start = index[is_start_idx]
            is_end = index[is_end_idx]
            oos_start = index[oos_start_idx]
            oos_end = index[oos_end_idx]

            is_features = [feature_pack_provider(ts) for ts in index[is_start_idx : is_end_idx + 1]]
            oos_features = [feature_pack_provider(ts) for ts in index[oos_start_idx : oos_end_idx + 1]]

            is_result = self.simulator.run(strategy, is_features, start=is_start, end=is_end)
            oos_result = self.simulator.run(strategy, oos_features, start=oos_start, end=oos_end)

            is_return = float(is_result.total_return)
            oos_return = float(oos_result.total_return)
            wfe = self._compute_wfe(is_return, oos_return)
            step = {
                "step_index": len(result.per_step),
                "is_start": str(is_start),
                "is_end": str(is_end),
                "oos_start": str(oos_start),
                "oos_end": str(oos_end),
                "is_return": is_return,
                "oos_return": oos_return,
                "wfe": wfe,
                "is_sharpe": float(is_result.metrics.get("sharpe", 0.0)),
                "oos_sharpe": float(oos_result.metrics.get("sharpe", 0.0)),
                "is_trades": int(is_result.metrics.get("n_trades", 0)),
                "oos_trades": int(oos_result.metrics.get("n_trades", 0)),
            }
            result.per_step.append(step)
            i += self.step_bars

        result.n_steps = len(result.per_step)
        if result.n_steps:
            passing = [
                1 for s in result.per_step if s["wfe"] >= self.wfe_threshold
            ]
            result.wfe_pass_ratio = float(sum(passing) / result.n_steps)
        return result

    # ----- helpers -----

    @staticmethod
    def _compute_wfe(is_return: float, oos_return: float) -> float:
        """Walk-forward efficiency.

        WFE = oos_return / is_return when both are positive (so a value of 1.0
        means OOS matched IS). If IS is non-positive, the strategy didn't have
        edge in-sample and WFE is undefined → return 0 (fail). If OOS is
        non-positive while IS positive, WFE = 0 (fail).
        """
        if is_return <= 0:
            return 0.0
        if oos_return <= 0:
            return 0.0
        return float(oos_return / is_return)
