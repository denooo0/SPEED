"""Strategy contract for the event-driven backtest.

A Strategy receives one bar at a time. It cannot peek into the future:
on_bar() is called *after* the bar closes; any returned Order is queued for the
*next* bar's open (with spread + slippage costs simulated).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional, Protocol, runtime_checkable

import pandas as pd


Direction = Literal["long", "short"]


@dataclass
class Bar:
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PositionState:
    """Snapshot of the open position presented to the strategy each bar.

    `is_open` is False when flat. For flat state, `direction`, `entry_price`,
    `stop_loss` are not meaningful (defaulted).
    """

    is_open: bool = False
    direction: Direction = "long"
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profits: tuple = ()
    size: float = 0.0
    bars_held: int = 0


@dataclass
class Order:
    direction: Direction
    entry_price: float  # the LIMIT level; actual fill is next-bar-open with spread
    stop_loss: float
    take_profits: list  # ordered (in price; ascending for longs, descending for shorts)
    size: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Strategy(Protocol):
    """The contract a backtest-able strategy must implement.

    Implementations should be pure functions of (bar, feature_pack, position_state).
    Strategies must NOT depend on future bars; that bias is the responsibility of
    the simulator to prevent.
    """

    def on_bar(
        self,
        bar: Bar,
        feature_pack: Dict[str, Any],
        position_state: PositionState,
    ) -> Optional[Order]:
        ...
