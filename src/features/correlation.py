"""Rolling correlation between XAUUSD and macro indicators.

The intent layer already covers funding/OI/COT. This module captures the
*cross-asset* posture: gold vs DXY, gold vs US10Y yield, gold vs SPX.

Classification rules:
    - "haven_bid"   : gold up & SPX down (XAU-SPX correlation strongly negative)
                       AND gold up & DXY down (XAU-DXY correlation negative)
    - "risk_on"     : SPX up & gold up & DXY down — broad reflation
    - "risk_off"    : SPX down & DXY up — flight-to-USD
    - "decoupled"   : weak correlations across the board (|all| < 0.2)
    - "unknown"     : insufficient data (e.g. no macro feeds provided)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Literal, Optional

import numpy as np
import pandas as pd


Regime = Literal["risk_on", "risk_off", "haven_bid", "decoupled", "unknown"]


@dataclass
class CorrelationFeatures:
    corr_dxy_30d: Optional[float] = None
    corr_us10y_30d: Optional[float] = None
    corr_spx_30d: Optional[float] = None
    regime_classification: Regime = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _to_daily_returns(series: pd.Series) -> pd.Series:
    s = series.dropna().astype(float)
    if s.empty:
        return s
    # Use percent-change of daily resampled close.
    daily = s.resample("1D").last().dropna()
    return daily.pct_change().dropna()


def _rolling_corr(
    gold: pd.Series,
    macro: pd.Series,
    window_days: int = 30,
) -> Optional[float]:
    """Last value of the `window_days` rolling correlation between two daily-return series."""
    if gold is None or macro is None:
        return None
    g = _to_daily_returns(gold)
    m = _to_daily_returns(macro)
    if g.empty or m.empty:
        return None
    aligned = pd.concat([g, m], axis=1, join="inner").dropna()
    if len(aligned) < max(5, window_days // 2):
        return None
    eff_window = min(window_days, len(aligned))
    corr_series = aligned.iloc[:, 0].rolling(window=eff_window).corr(aligned.iloc[:, 1])
    corr_series = corr_series.dropna()
    if corr_series.empty:
        return None
    val = float(corr_series.iloc[-1])
    if np.isnan(val):
        return None
    return val


def _classify(
    corr_dxy: Optional[float],
    corr_us10y: Optional[float],
    corr_spx: Optional[float],
    gold_recent_dir: Optional[int] = None,
) -> Regime:
    available = [c for c in (corr_dxy, corr_us10y, corr_spx) if c is not None]
    if not available:
        return "unknown"

    weak = [abs(c) < 0.2 for c in available]
    if all(weak):
        return "decoupled"

    # Haven bid: gold inversely correlated to BOTH DXY and SPX (classic safe-haven).
    if corr_dxy is not None and corr_spx is not None:
        if corr_dxy < -0.3 and corr_spx < -0.3:
            return "haven_bid"

    # Risk-on: gold-DXY negative (USD weakness) AND gold-SPX positive.
    if corr_dxy is not None and corr_spx is not None:
        if corr_dxy < -0.2 and corr_spx > 0.2:
            return "risk_on"

    # Risk-off: gold-DXY positive AND gold-SPX negative.
    if corr_dxy is not None and corr_spx is not None:
        if corr_dxy > 0.2 and corr_spx < -0.2:
            return "risk_off"

    return "decoupled"


def extract(
    candles_xau: pd.DataFrame,
    macro_series: Optional[Dict[str, pd.DataFrame]] = None,
    window_days: int = 30,
) -> CorrelationFeatures:
    """Compute correlation snapshot.

    `candles_xau` must have a DatetimeIndex and a `close` column.
    `macro_series` keys are recognized: "DXY", "US10Y", "SPX". Each must be a
    DataFrame with DatetimeIndex and a `close` column. Returns all-None +
    `unknown` when `macro_series` is missing.
    """
    if macro_series is None or not macro_series:
        return CorrelationFeatures()

    if candles_xau is None or candles_xau.empty or "close" not in candles_xau.columns:
        return CorrelationFeatures()

    gold = candles_xau["close"]

    def _maybe(name: str) -> Optional[float]:
        df = macro_series.get(name)
        if df is None or df.empty or "close" not in df.columns:
            return None
        return _rolling_corr(gold, df["close"], window_days=window_days)

    corr_dxy = _maybe("DXY")
    corr_us10y = _maybe("US10Y")
    corr_spx = _maybe("SPX")

    regime = _classify(corr_dxy, corr_us10y, corr_spx)

    return CorrelationFeatures(
        corr_dxy_30d=corr_dxy,
        corr_us10y_30d=corr_us10y,
        corr_spx_30d=corr_spx,
        regime_classification=regime,
    )
