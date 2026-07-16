"""HTF Retracement Short — CONFLUENCE-FILTERED.

Built from the empirical winner-vs-loser analysis of the naive baseline
(scripts/analyze_trades.py on 10yr gold H1). The naive rule was 1pp below
breakeven with 1,132 noisy trades. This version keeps the same entry pattern
but only fires when the measured confluences align, trading quality over
quantity:

  1. Secular regime: price below the slow MA (MA200) — only short genuine
     bearish regimes (naive baseline: 35.8% win below MA200 vs 32% overall).
  2. Trend strength: MA50 declining by at least `min_downslope_bp` over the
     lookback (steep downtrends win more).
  3. Entry location: the retracement closes BELOW the declining MA50 (trend
     intact). Deep retraces that push above the MA are reversals — 0% win in
     the data — and are excluded.
  4. Momentum: recent N-bar return is negative past `min_recent_down_bp`
     (sell continuation, not drift — the standout edge: 55% win when strongly
     down).
  5. Session: optionally skip the NY-London overlap (worst win rate in the
     data — that's when reversals fire).

Still deterministic and self-contained (own rolling state, no feature pack).
This is the confluence layer the operator applies discretionarily, made
mechanical. Its edge must still clear the walk-forward + CPCV + DSR gate; that
is what tells us the confluences generalize rather than overfit.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, Optional

from src.backtest.strategy import Bar, Order, PositionState, Strategy


class HtfRetracementShortFiltered(Strategy):
    def __init__(
        self,
        ma_fast: int = 50,
        ma_slow: int = 200,
        slope_lookback: int = 20,
        min_downslope_bp: float = 50.0,       # MA50 must fall at least this over lookback
        min_recent_down_bp: float = 50.0,     # ret over `mom_lookback` bars must be <= -this
        mom_lookback: int = 20,
        require_below_slow: bool = True,       # price below MA200
        require_close_below_fast: bool = True, # entry below MA50 (trend intact)
        skip_ny_london_overlap: bool = True,   # 13:00-16:00 UTC excluded
        rr_target: float = 2.0,
        stop_buffer_bp: float = 10.0,
    ) -> None:
        self.ma_fast = int(ma_fast)
        self.ma_slow = int(ma_slow)
        self.slope_lookback = int(slope_lookback)
        self.min_downslope_bp = float(min_downslope_bp)
        self.min_recent_down_bp = float(min_recent_down_bp)
        self.mom_lookback = int(mom_lookback)
        self.require_below_slow = bool(require_below_slow)
        self.require_close_below_fast = bool(require_close_below_fast)
        self.skip_overlap = bool(skip_ny_london_overlap)
        self.rr_target = float(rr_target)
        self.stop_buffer = stop_buffer_bp / 10_000.0

        maxlen = max(self.ma_slow, self.ma_fast + self.slope_lookback, self.mom_lookback) + 5
        self._closes: Deque[float] = deque(maxlen=maxlen)
        self._ma_fast_hist: Deque[float] = deque(maxlen=self.slope_lookback + 2)

    def _sma(self, period: int) -> Optional[float]:
        if len(self._closes) < period:
            return None
        window = list(self._closes)[-period:]
        return sum(window) / period

    def on_bar(
        self,
        bar: Bar,
        feature_pack: Dict[str, Any],
        position_state: PositionState,
    ) -> Optional[Order]:
        self._closes.append(bar.close)
        ma_fast = self._sma(self.ma_fast)
        if ma_fast is not None:
            self._ma_fast_hist.append(ma_fast)

        if position_state.is_open or ma_fast is None:
            return None
        ma_slow = self._sma(self.ma_slow)
        if ma_slow is None:
            return None

        # --- Confluence 1: secular bearish regime ---
        if self.require_below_slow and bar.close >= ma_slow:
            return None

        # --- Confluence 2: MA50 declining steeply enough ---
        if len(self._ma_fast_hist) < self.slope_lookback:
            return None
        slope_bp = (self._ma_fast_hist[-1] - self._ma_fast_hist[0]) / self._ma_fast_hist[-1] * 10_000
        if slope_bp > -self.min_downslope_bp:  # not declining enough
            return None

        # --- Confluence 3: retracement touched the declining MA and got rejected ---
        touched = bar.high >= ma_fast * (1.0 - 0.0015)  # within 15bp of MA or above
        if not touched:
            return None
        if self.require_close_below_fast and bar.close >= ma_fast:
            return None  # closed above MA = reversal risk (0% win in the data)

        # --- Confluence 4: recent momentum is down (sell continuation) ---
        if len(self._closes) > self.mom_lookback:
            ref = list(self._closes)[-(self.mom_lookback + 1)]
            ret_bp = (bar.close - ref) / ref * 10_000
            if ret_bp > -self.min_recent_down_bp:
                return None

        # --- Confluence 5: avoid the NY-London overlap (worst win rate) ---
        if self.skip_overlap:
            hour = getattr(bar.timestamp, "hour", None)
            if hour is not None and 13 <= hour < 16:
                return None

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


def make_strategy(hypothesis: Dict[str, Any]) -> HtfRetracementShortFiltered:
    params = (hypothesis or {}).get("params", {}) if isinstance(hypothesis, dict) else {}
    return HtfRetracementShortFiltered(
        ma_fast=int(params.get("ma_fast", 50)),
        ma_slow=int(params.get("ma_slow", 200)),
        slope_lookback=int(params.get("slope_lookback", 20)),
        min_downslope_bp=float(params.get("min_downslope_bp", 50.0)),
        min_recent_down_bp=float(params.get("min_recent_down_bp", 50.0)),
        mom_lookback=int(params.get("mom_lookback", 20)),
        require_below_slow=bool(params.get("require_below_slow", True)),
        require_close_below_fast=bool(params.get("require_close_below_fast", True)),
        skip_ny_london_overlap=bool(params.get("skip_ny_london_overlap", True)),
        rr_target=float(params.get("rr_target", 2.0)),
        stop_buffer_bp=float(params.get("stop_buffer_bp", 10.0)),
    )
