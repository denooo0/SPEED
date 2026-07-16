"""Momentum Continuation — trade WITH strong momentum in the trend direction.

Built from Experiment 001's carry-forward learnings:
  1. Trade WITH momentum, not fading it. The standout signal in the trade
     mining was "strong recent down-momentum -> 55% win". This strategy enters
     in the direction of a strong recent move that is also aligned with the
     trend (price vs MA), on a breakout of the recent range (continuation).
  2. Use targets winners can actually reach. Experiment 001 showed a 2R target
     is too far (winners captured ~1.2R). Default here is modest (1R) with an
     ATR-based stop, and it's swept.
  3. Symmetric long + short. The recent data is a secular gold bull
     (2023->2025 +84%), so a short-only rule fights the tide. The momentum
     MECHANISM is direction-agnostic; we test it both ways, then can specialize
     to the short side for the operator's downtrend setups.

Deterministic, self-contained (own rolling state; no feature pack). Must still
clear walk-forward + CPCV + DSR to count as edge.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, Optional

from src.backtest.strategy import Bar, Order, PositionState, Strategy


class MomentumContinuation(Strategy):
    def __init__(
        self,
        ma_period: int = 50,
        mom_lookback: int = 20,
        min_mom_bp: float = 100.0,        # required |return| over mom_lookback bars (bp)
        breakout_lookback: int = 10,      # new N-bar extreme = continuation trigger
        atr_period: int = 14,
        stop_atr_mult: float = 1.5,
        rr_target: float = 1.0,
        min_stop_bp: float = 25.0,
        allow_long: bool = True,
        allow_short: bool = True,
    ) -> None:
        self.ma_period = int(ma_period)
        self.mom_lookback = int(mom_lookback)
        self.min_mom_bp = float(min_mom_bp)
        self.breakout_lookback = int(breakout_lookback)
        self.atr_period = int(atr_period)
        self.stop_atr_mult = float(stop_atr_mult)
        self.rr_target = float(rr_target)
        self.min_stop_bp = float(min_stop_bp)
        self.allow_long = bool(allow_long)
        self.allow_short = bool(allow_short)

        maxlen = max(self.ma_period, self.mom_lookback, self.breakout_lookback, self.atr_period) + 5
        self._closes: Deque[float] = deque(maxlen=maxlen)
        self._highs: Deque[float] = deque(maxlen=maxlen)
        self._lows: Deque[float] = deque(maxlen=maxlen)
        self._prev_close: Optional[float] = None
        self._trs: Deque[float] = deque(maxlen=self.atr_period + 2)

    def _sma(self) -> Optional[float]:
        if len(self._closes) < self.ma_period:
            return None
        window = list(self._closes)[-self.ma_period :]
        return sum(window) / self.ma_period

    def _atr(self) -> Optional[float]:
        if len(self._trs) < self.atr_period:
            return None
        window = list(self._trs)[-self.atr_period :]
        return sum(window) / self.atr_period

    def on_bar(
        self,
        bar: Bar,
        feature_pack: Dict[str, Any],
        position_state: PositionState,
    ) -> Optional[Order]:
        # Update rolling state (true range needs prev close).
        if self._prev_close is not None:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
            self._trs.append(tr)
        self._prev_close = bar.close
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)

        if position_state.is_open:
            return None
        ma = self._sma()
        atr = self._atr()
        if ma is None or atr is None or atr <= 0:
            return None
        if len(self._closes) <= self.mom_lookback or len(self._highs) <= self.breakout_lookback:
            return None

        ref = list(self._closes)[-(self.mom_lookback + 1)]
        mom_bp = (bar.close - ref) / ref * 10_000

        # prior N-bar extremes (exclude the current bar).
        prior_high = max(list(self._highs)[-(self.breakout_lookback + 1) : -1])
        prior_low = min(list(self._lows)[-(self.breakout_lookback + 1) : -1])

        # LONG: uptrend + strong up momentum + breakout to new high.
        if (
            self.allow_long
            and bar.close > ma
            and mom_bp >= self.min_mom_bp
            and bar.close > prior_high
        ):
            entry = bar.close
            stop = entry - max(self.stop_atr_mult * atr, entry * self.min_stop_bp / 10_000.0)
            tp = entry + self.rr_target * (entry - stop)
            if entry - stop > 0:
                return Order(direction="long", entry_price=entry, stop_loss=stop,
                             take_profits=[tp], size=1.0)

        # SHORT: downtrend + strong down momentum + breakdown to new low.
        if (
            self.allow_short
            and bar.close < ma
            and mom_bp <= -self.min_mom_bp
            and bar.close < prior_low
        ):
            entry = bar.close
            stop = entry + max(self.stop_atr_mult * atr, entry * self.min_stop_bp / 10_000.0)
            tp = entry - self.rr_target * (stop - entry)
            if stop - entry > 0:
                return Order(direction="short", entry_price=entry, stop_loss=stop,
                             take_profits=[tp], size=1.0)
        return None


def make_strategy(hypothesis: Dict[str, Any]) -> MomentumContinuation:
    p = (hypothesis or {}).get("params", {}) if isinstance(hypothesis, dict) else {}
    return MomentumContinuation(
        ma_period=int(p.get("ma_period", 50)),
        mom_lookback=int(p.get("mom_lookback", 20)),
        min_mom_bp=float(p.get("min_mom_bp", 100.0)),
        breakout_lookback=int(p.get("breakout_lookback", 10)),
        atr_period=int(p.get("atr_period", 14)),
        stop_atr_mult=float(p.get("stop_atr_mult", 1.5)),
        rr_target=float(p.get("rr_target", 1.0)),
        min_stop_bp=float(p.get("min_stop_bp", 25.0)),
        allow_long=bool(p.get("allow_long", True)),
        allow_short=bool(p.get("allow_short", True)),
    )
