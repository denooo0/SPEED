"""Event-driven backtest simulator.

Design contract:
    * Strategy.on_bar() is called exactly once per bar, in chronological order,
      AFTER the bar has closed. The strategy sees the closing OHLCV plus the
      feature_pack snapshot computed for that bar.
    * If Strategy.on_bar() returns an Order, the order is filled on the NEXT
      bar's open (look-ahead bias is forbidden).
    * The fill price applies:
        - half-spread (in bps of the level) crossing
        - latency drift: a fraction `latency_seconds / bar_seconds` of the
          NEXT bar's worst-case move (high for longs, low for shorts) is
          applied on top of next_bar_open
    * Once a position is open, each subsequent bar is checked for SL/TP
      using the bar's OHLC. If the bar can hit both SL and TP, we assume
      the WORST-case order (SL first) — the standard conservative rule.
    * Exit fill prices also have spread + slippage applied (subtracted from
      gross PnL via the cost model).
    * Funding drag is applied proportional to the hold duration.

The simulator yields a BacktestResult containing the trade ledger, the
equity curve (in account currency, base 1.0) and a metrics dict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

import numpy as np
import pandas as pd

from src.backtest.cost_model import CostModel
from src.backtest.strategy import Bar, Order, PositionState, Strategy


def _bps(amount_bps: float) -> float:
    """Convert basis points to a multiplicative fraction (e.g. 10bp -> 0.001)."""
    return amount_bps / 10_000.0


@dataclass
class _OpenPosition:
    direction: str  # "long" / "short"
    entry_price: float          # net entry incl. spread + slippage (the realized fill)
    entry_time: pd.Timestamp
    stop_loss: float
    take_profits: List[float]
    size: float
    bars_held: int = 0
    news_window_entry: bool = False
    risk_per_unit: float = 0.0   # |entry - stop_loss|, used for R-multiple math
    entry_cost_bps: float = 0.0  # remembered so exit accounting is symmetric


@dataclass
class BacktestResult:
    """Output of a single backtest run."""

    trades: pd.DataFrame
    equity_curve: pd.Series
    metrics: Dict[str, float]
    config: Dict[str, Any]

    # ---- convenience accessors -------------------------------------------

    @property
    def n_trades(self) -> int:
        return int(len(self.trades))

    @property
    def total_return(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        return float(self.equity_curve.iloc[-1] / self.equity_curve.iloc[0] - 1.0)


class EventDrivenSimulator:
    """Single-instrument, event-driven backtest simulator.

    Parameters
    ----------
    candles:
        DataFrame indexed by timestamp, columns: open, high, low, close, volume.
    cost_model:
        CostModel instance describing fee/spread/slippage/funding.
    is_news_window:
        Callable(timestamp) -> bool, marks bars where wide-spread regime applies.
    bar_seconds:
        Number of seconds per bar. Used to compute the latency fraction within a bar.
        Defaults to 5 minutes (300s).
    initial_equity:
        Starting equity in account units. Default 1.0 (so equity_curve is multiplicative).
    """

    def __init__(
        self,
        candles: pd.DataFrame,
        cost_model: CostModel,
        is_news_window: Callable[[pd.Timestamp], bool] = lambda _ts: False,
        bar_seconds: int = 300,
        initial_equity: float = 1.0,
    ) -> None:
        if candles.empty:
            raise ValueError("candles must be non-empty")
        required = {"open", "high", "low", "close", "volume"}
        missing = required.difference(candles.columns)
        if missing:
            raise ValueError(f"candles missing columns: {sorted(missing)}")
        if not candles.index.is_monotonic_increasing:
            raise ValueError("candles index must be monotonic increasing")

        self.candles = candles
        self.cost_model = cost_model
        self.is_news_window = is_news_window
        self.bar_seconds = int(bar_seconds)
        if self.bar_seconds <= 0:
            raise ValueError("bar_seconds must be positive")
        self.initial_equity = float(initial_equity)

    # ---------------------------------------------------------------- run

    def run(
        self,
        strategy: Strategy,
        feature_pack_iter: Iterable[Dict[str, Any]],
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> BacktestResult:
        """Run the strategy on the (optionally sliced) candle range.

        feature_pack_iter:
            Sequence of dicts, one per bar in the (sliced) range, in order.
            If shorter than the candle range, missing bars receive an empty dict.
        """
        df = self.candles
        if start is not None:
            df = df.loc[df.index >= start]
        if end is not None:
            df = df.loc[df.index <= end]
        if df.empty:
            return self._empty_result(start, end)

        feature_packs = list(feature_pack_iter) if feature_pack_iter is not None else []

        position: Optional[_OpenPosition] = None
        pending_order: Optional[Order] = None
        equity = self.initial_equity
        equity_points: List[tuple] = [(df.index[0], equity)]
        trade_rows: List[Dict[str, Any]] = []

        index = df.index
        opens = df["open"].to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        volumes = df["volume"].to_numpy(dtype=float)
        n = len(df)

        for i in range(n):
            ts = index[i]
            bar = Bar(
                timestamp=ts,
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(volumes[i]),
            )

            # ------------------------- 1. Fill any pending order at THIS bar's open
            if pending_order is not None and position is None:
                position = self._fill_order(pending_order, bar)
                pending_order = None

            # ------------------------- 2. Evaluate open position against this bar
            if position is not None:
                exit_event = self._check_exits(position, bar)
                if exit_event is not None:
                    pnl_quote, r_mult = self._close_position(
                        position, exit_event["price"], exit_event["time"], bar
                    )
                    equity *= 1.0 + pnl_quote
                    hold_hours = self._hold_hours(position.entry_time, exit_event["time"])
                    trade_rows.append(
                        {
                            "entry_time": position.entry_time,
                            "exit_time": exit_event["time"],
                            "direction": position.direction,
                            "entry_price": position.entry_price,
                            "exit_price": exit_event["price"],
                            "size": position.size,
                            "pnl": pnl_quote,
                            "r_multiple": r_mult,
                            "hold_hours": hold_hours,
                            "exit_reason": exit_event["reason"],
                        }
                    )
                    position = None
                else:
                    position.bars_held += 1

            # ------------------------- 3. Strategy sees the bar AFTER any exits
            feature_pack = feature_packs[i] if i < len(feature_packs) else {}
            pos_state = self._snapshot(position)
            order = strategy.on_bar(bar, feature_pack, pos_state)

            if order is not None and position is None and pending_order is None:
                pending_order = order
            elif order is not None and (position is not None or pending_order is not None):
                # Silently ignore — one trade at a time.
                pass

            equity_points.append((ts, equity))

        # End of data: force-close any open position at the last close.
        if position is not None:
            last_ts = index[-1]
            last_close = float(closes[-1])
            pnl_quote, r_mult = self._close_position(position, last_close, last_ts, None)
            equity *= 1.0 + pnl_quote
            hold_hours = self._hold_hours(position.entry_time, last_ts)
            trade_rows.append(
                {
                    "entry_time": position.entry_time,
                    "exit_time": last_ts,
                    "direction": position.direction,
                    "entry_price": position.entry_price,
                    "exit_price": last_close,
                    "size": position.size,
                    "pnl": pnl_quote,
                    "r_multiple": r_mult,
                    "hold_hours": hold_hours,
                    "exit_reason": "EOD",
                }
            )
            equity_points[-1] = (index[-1], equity)
            position = None

        trades_df = pd.DataFrame(trade_rows)
        eq_idx = [p[0] for p in equity_points]
        eq_vals = [p[1] for p in equity_points]
        equity_curve = pd.Series(eq_vals, index=pd.Index(eq_idx, name="timestamp"), name="equity")

        # Lazy import to avoid circular reference at module load.
        from src.backtest.metrics import summarize

        metrics = summarize(trades_df, equity_curve, bar_seconds=self.bar_seconds)
        config = {
            "cost_model": self.cost_model.__dict__.copy(),
            "bar_seconds": self.bar_seconds,
            "initial_equity": self.initial_equity,
            "start": str(start) if start is not None else None,
            "end": str(end) if end is not None else None,
            "candles_used": int(n),
        }
        return BacktestResult(trades=trades_df, equity_curve=equity_curve, metrics=metrics, config=config)

    # ----------------------------------------------------- internals

    def _empty_result(
        self, start: Optional[pd.Timestamp], end: Optional[pd.Timestamp]
    ) -> BacktestResult:
        empty_trades = pd.DataFrame(
            columns=[
                "entry_time",
                "exit_time",
                "direction",
                "entry_price",
                "exit_price",
                "size",
                "pnl",
                "r_multiple",
                "hold_hours",
                "exit_reason",
            ]
        )
        return BacktestResult(
            trades=empty_trades,
            equity_curve=pd.Series(dtype=float),
            metrics={},
            config={"start": str(start), "end": str(end), "candles_used": 0},
        )

    def _snapshot(self, position: Optional[_OpenPosition]) -> PositionState:
        if position is None:
            return PositionState()
        return PositionState(
            is_open=True,
            direction=position.direction,
            entry_price=position.entry_price,
            stop_loss=position.stop_loss,
            take_profits=tuple(position.take_profits),
            size=position.size,
            bars_held=position.bars_held,
        )

    def _hold_hours(self, t_in: pd.Timestamp, t_out: pd.Timestamp) -> float:
        delta = (t_out - t_in).total_seconds()
        return float(delta) / 3600.0

    # ----- order fill (called at start of NEXT bar) -----

    def _fill_order(self, order: Order, fill_bar: Bar) -> _OpenPosition:
        news = bool(self.is_news_window(fill_bar.timestamp))
        entry_cost_bps = self.cost_model.entry_cost_bps(
            side=order.direction, news_window=news, order_type="taker"
        )
        # Latency drift: fraction of the bar elapsed before our order touches book.
        latency_frac = min(1.0, self.cost_model.latency_seconds / float(self.bar_seconds))
        # Worst-case fill price adjusted for latency: long pays up toward bar high,
        # short receives down toward bar low.
        if order.direction == "long":
            latency_drift_price = (fill_bar.high - fill_bar.open) * latency_frac
            base_fill = fill_bar.open + latency_drift_price
            # spread + slippage charged in bps of the base level
            net_entry = base_fill * (1.0 + _bps(entry_cost_bps))
            risk_per_unit = max(net_entry - order.stop_loss, 1e-12)
        else:
            latency_drift_price = (fill_bar.open - fill_bar.low) * latency_frac
            base_fill = fill_bar.open - latency_drift_price
            net_entry = base_fill * (1.0 - _bps(entry_cost_bps))
            risk_per_unit = max(order.stop_loss - net_entry, 1e-12)

        return _OpenPosition(
            direction=order.direction,
            entry_price=net_entry,
            entry_time=fill_bar.timestamp,
            stop_loss=order.stop_loss,
            take_profits=list(order.take_profits),
            size=order.size,
            bars_held=0,
            news_window_entry=news,
            risk_per_unit=risk_per_unit,
            entry_cost_bps=entry_cost_bps,
        )

    # ----- exit detection within a single bar -----

    def _check_exits(self, position: _OpenPosition, bar: Bar) -> Optional[Dict[str, Any]]:
        """Determine if SL or any TP triggers within this bar's range.

        Conservative ordering: when both SL and TP can hit within the same bar,
        SL is assumed to fire first (worst-case).
        """
        sl = position.stop_loss
        # Use the FINAL (most-distant) take profit if there are multiple.
        tps = position.take_profits
        if position.direction == "long":
            hit_sl = bar.low <= sl
            tp_hit_price = None
            if tps:
                # For longs, the first TP that can be reached within the bar's high.
                for tp in tps:
                    if bar.high >= tp:
                        tp_hit_price = tp
                        break
            if hit_sl:
                return {"reason": "SL", "price": sl, "time": bar.timestamp}
            if tp_hit_price is not None:
                return {"reason": "TP", "price": tp_hit_price, "time": bar.timestamp}
        else:  # short
            hit_sl = bar.high >= sl
            tp_hit_price = None
            if tps:
                for tp in tps:
                    if bar.low <= tp:
                        tp_hit_price = tp
                        break
            if hit_sl:
                return {"reason": "SL", "price": sl, "time": bar.timestamp}
            if tp_hit_price is not None:
                return {"reason": "TP", "price": tp_hit_price, "time": bar.timestamp}
        return None

    # ----- compute pnl on close -----

    def _close_position(
        self,
        position: _OpenPosition,
        raw_exit_price: float,
        exit_time: pd.Timestamp,
        exit_bar: Optional[Bar],
    ) -> tuple:
        """Compute net PnL (as a fraction of equity-1-unit) for closing.

        Returns (pnl_fraction, r_multiple).

        The 'pnl_fraction' returned is the realized return on the trade's notional
        with size=1 baseline, multiplied by position.size. Caller multiplies this
        into the running equity multiplicatively.
        """
        news = bool(self.is_news_window(exit_time))
        exit_cost_bps = self.cost_model.exit_cost_bps(
            side=position.direction, news_window=news, order_type="taker"
        )
        if position.direction == "long":
            net_exit = raw_exit_price * (1.0 - _bps(exit_cost_bps))
            gross_return = (net_exit - position.entry_price) / position.entry_price
        else:
            net_exit = raw_exit_price * (1.0 + _bps(exit_cost_bps))
            gross_return = (position.entry_price - net_exit) / position.entry_price

        # Funding drag in bps of notional, applied to the gross return as additive bps.
        hold_hours = self._hold_hours(position.entry_time, exit_time)
        funding_bps = self.cost_model.funding_drag_bps(hold_hours, position.direction)
        gross_return -= _bps(funding_bps)

        # r-multiple: compare to per-unit risk at entry.
        if position.risk_per_unit > 0:
            if position.direction == "long":
                price_move = raw_exit_price - position.entry_price
            else:
                price_move = position.entry_price - raw_exit_price
            r_mult = price_move / position.risk_per_unit
        else:
            r_mult = 0.0

        pnl_fraction = gross_return * position.size
        return pnl_fraction, r_mult
