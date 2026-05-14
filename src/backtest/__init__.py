"""Event-driven backtest harness for ATLAS XAUUSD.

Modules:
    cost_model     - realistic Bybit fee/spread/slippage/funding model
    strategy       - Strategy Protocol + Bar/Order/PositionState dataclasses
    simulator      - EventDrivenSimulator (next-bar-open fills, no look-ahead)
    metrics        - Sharpe, Sortino, MaxDD, Calmar, DSR, PBO, ruin probability
    walk_forward   - rolling walk-forward with WFE per step
    cpcv           - combinatorial purged cross-validation
    runner         - top-level evaluate_hypothesis() entry point
"""
from __future__ import annotations

from src.backtest.cost_model import CostModel  # noqa: F401
from src.backtest.strategy import Bar, Order, PositionState, Strategy  # noqa: F401
from src.backtest.simulator import BacktestResult, EventDrivenSimulator  # noqa: F401
from src.backtest.walk_forward import WalkForwardResult, WalkForwardRunner  # noqa: F401
from src.backtest.cpcv import CombinatorialPurgedCV, CPCVResult  # noqa: F401
from src.backtest.runner import BacktestRunner  # noqa: F401
