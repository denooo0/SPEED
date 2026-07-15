"""HTF Retracement Continuation (short) — the operator's demonstrated edge.

Encodes, as a DETERMINISTIC strategy, the pattern from the two live XAUUSD
shorts (4-lot @ 4128, 1-lot @ 4058): in a higher-timeframe downtrend, a
counter-trend push UP that stalls at the declining moving average is faded
short, with the stop above the retracement high and a fixed R target.

This is the deterministic BASELINE. The LLM is a separate FILTER layer
(src/backtest/llm_filter.py) applied on top later. Per the research findings,
we prove the deterministic edge first — if the raw pattern has no edge, no
LLM filter can manufacture one.

Pure function of (bar, feature_pack, position_state). No future data. The
simulator enforces next-bar-open fills.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, Optional

from src.backtest.strategy import Bar, Order, PositionState, Strategy


class HtfRetracementShort(Strategy):
    """Sell the retracement into a declining MA in a downtrend.

    Parameters
    ----------
    ma_period:
        Moving-average period used as the "declining MA" proxy (on this
        timeframe's closes).
    trend_lookback:
        Bars over which the MA must be declining to call the HTF bearish.
    touch_tolerance_bp:
        How close (in basis points) the bar's high must come to the MA to
        count as a "touch / retracement into the MA".
    rr_target:
        Reward-to-risk multiple for the take-profit.
    stop_buffer_bp:
        Extra buffer above the retracement high for the stop, in bp.
    """

    def __init__(
        self,
        ma_period: int = 50,
        trend_lookback: int = 10,
        touch_tolerance_bp: float = 15.0,
        rr_target: float = 2.0,
        stop_buffer_bp: float = 10.0,
    ) -> None:
        self.ma_period = int(ma_period)
        self.trend_lookback = int(trend_lookback)
        self.touch_tol = touch_tolerance_bp / 10_000.0
        self.rr_target = float(rr_target)
        self.stop_buffer = stop_buffer_bp / 10_000.0

        self._closes: Deque[float] = deque(maxlen=self.ma_period + self.trend_lookback + 2)
        self._ma_hist: Deque[float] = deque(maxlen=self.trend_lookback + 2)

    def _ma(self) -> Optional[float]:
        if len(self._closes) < self.ma_period:
            return None
        window = list(self._closes)[-self.ma_period :]
        return sum(window) / self.ma_period

    def _ma_declining(self) -> bool:
        if len(self._ma_hist) < self.trend_lookback:
            return False
        recent = list(self._ma_hist)[-self.trend_lookback :]
        # Strictly-ish declining: last well below first, monotone-ish.
        return recent[-1] < recent[0] and recent[-1] < recent[len(recent) // 2]

    def on_bar(
        self,
        bar: Bar,
        feature_pack: Dict[str, Any],
        position_state: PositionState,
    ) -> Optional[Order]:
        self._closes.append(bar.close)
        ma = self._ma()
        if ma is not None:
            self._ma_hist.append(ma)

        if position_state.is_open or ma is None:
            return None
        if not self._ma_declining():
            return None

        # HTF bearish (declining MA) confirmed. Now: did THIS bar retrace UP into
        # the MA and get rejected? Conditions:
        #   - bar traded up to / above the MA (high within tolerance of MA or above)
        #   - but closed back BELOW the MA (rejection)
        #   - price is in a downtrend (close below MA)
        touched_ma = bar.high >= ma * (1.0 - self.touch_tol)
        rejected = bar.close < ma
        if not (touched_ma and rejected):
            return None

        # Entry at the close; stop above the retracement high; target rr_target * risk.
        entry_ref = bar.close
        stop = bar.high * (1.0 + self.stop_buffer)
        risk = stop - entry_ref
        if risk <= 0:
            return None
        tp = entry_ref - self.rr_target * risk
        return Order(
            direction="short",
            entry_price=entry_ref,
            stop_loss=stop,
            take_profits=[tp],
            size=1.0,
        )


def make_strategy(hypothesis: Dict[str, Any]) -> HtfRetracementShort:
    """Factory for BacktestRunner. Reads optional params from the hypothesis dict."""
    params = (hypothesis or {}).get("params", {}) if isinstance(hypothesis, dict) else {}
    return HtfRetracementShort(
        ma_period=int(params.get("ma_period", 50)),
        trend_lookback=int(params.get("trend_lookback", 10)),
        touch_tolerance_bp=float(params.get("touch_tolerance_bp", 15.0)),
        rr_target=float(params.get("rr_target", 2.0)),
        stop_buffer_bp=float(params.get("stop_buffer_bp", 10.0)),
    )
