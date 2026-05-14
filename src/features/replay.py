"""Replay-mode feature extraction.

The 4-lens feature extractors in ``flow.py``, ``structure.py``, ``context.py``,
``intent.py`` operate on Python lists of candle dicts because the live trading
code carries data around as plain dicts (cheap, no pandas dep on the hot path).

For backtest replay we need to walk a long ``pandas.DataFrame`` of candles bar
by bar, slice the history up to *and including* the current bar, then drive
each of the lenses against that slice. ``FeatureReplayer`` does exactly this
and packages the result, together with the new order-flow / vol-regime /
correlation features, into a single ``FeaturePack`` snapshot per bar that
Track B's backtest simulator consumes.

The replayer **never reimplements** lens math — it calls the existing
``extract`` functions. That keeps live and replay paths in lock-step and makes
it trivial to reason about regressions.

Expected candle DataFrame schema:
    index:   pandas DatetimeIndex (tz-aware UTC preferred)
    columns: ['open', 'high', 'low', 'close', 'volume']

Macro DataFrames (optional, for the correlation lens):
    index:   pandas DatetimeIndex
    columns: at minimum ['close']
    keys recognized: 'DXY', 'US10Y', 'SPX'
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import numpy as np
import pandas as pd

from src.features import context as context_feat
from src.features import flow as flow_feat
from src.features import intent as intent_feat
from src.features import structure as structure_feat
from src.features.context import ContextFeatures
from src.features.correlation import CorrelationFeatures
from src.features.correlation import extract as correlation_extract
from src.features.flow import FlowFeatures
from src.features.intent import IntentFeatures
from src.features.order_flow import OrderFlowFeatures
from src.features.order_flow import extract as order_flow_extract
from src.features.structure import StructureFeatures
from src.features.vol_regime import VolRegimeFeatures
from src.features.vol_regime import extract as vol_regime_extract


# ---------------------------------------------------------------------------
# FeaturePack
# ---------------------------------------------------------------------------
@dataclass
class FeaturePack:
    """Structured 4-lens snapshot at a specific timestamp.

    Designed to be consumed by Track B's backtest simulator. ``to_dict`` is
    JSON/Pydantic-friendly (timestamps are ISO strings).
    """

    timestamp: pd.Timestamp
    flow: FlowFeatures
    structure: StructureFeatures
    context: ContextFeatures
    intent: IntentFeatures
    order_flow: OrderFlowFeatures
    vol_regime: VolRegimeFeatures
    correlation: CorrelationFeatures

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": (
                self.timestamp.isoformat()
                if isinstance(self.timestamp, pd.Timestamp)
                else str(self.timestamp)
            ),
            "flow": self.flow.to_dict(),
            "structure": self.structure.to_dict(),
            "context": self.context.to_dict(),
            "intent": self.intent.to_dict(),
            "order_flow": self.order_flow.to_dict(),
            "vol_regime": self.vol_regime.to_dict(),
            "correlation": self.correlation.to_dict(),
        }


# ---------------------------------------------------------------------------
# Helpers — DataFrame -> candle list bridge
# ---------------------------------------------------------------------------
_REQUIRED_COLS = ("open", "high", "low", "close", "volume")


def _ensure_datetime_index(df: pd.DataFrame, name: str) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError(f"{name} must have a pandas.DatetimeIndex (got {type(df.index).__name__})")
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")
    return df


def _df_to_candles(df: pd.DataFrame) -> List[Dict[str, float]]:
    """Convert an OHLCV DataFrame slice to the list-of-dicts shape the lenses expect.

    ``timestamp`` is emitted as milliseconds-since-epoch because the existing
    extractors (e.g. ``context.extract``) assume that convention.
    """
    if df.empty:
        return []
    # Vectorised conversion is materially faster than .iterrows for big slices.
    idx_ns = df.index.view("int64")  # nanoseconds since epoch
    ts_ms = (idx_ns // 1_000_000).astype("int64")
    opens = df["open"].to_numpy(dtype=float, copy=False)
    highs = df["high"].to_numpy(dtype=float, copy=False)
    lows = df["low"].to_numpy(dtype=float, copy=False)
    closes = df["close"].to_numpy(dtype=float, copy=False)
    vols = df["volume"].to_numpy(dtype=float, copy=False)
    return [
        {
            "timestamp": int(ts_ms[i]),
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
            "volume": float(vols[i]),
        }
        for i in range(len(df))
    ]


def _infer_timeframe_minutes(df: pd.DataFrame, default: int = 5) -> int:
    if len(df) < 2:
        return default
    deltas = df.index.to_series().diff().dropna()
    if deltas.empty:
        return default
    # mode → robust to occasional gaps (weekends/holidays).
    try:
        mode = deltas.mode().iloc[0]
    except IndexError:
        return default
    minutes = int(round(mode.total_seconds() / 60.0))
    return max(1, minutes)


def _cot_snapshot_at(cot_history: Optional[pd.DataFrame], ts: pd.Timestamp) -> Optional[Any]:
    """Pick the latest COT row at or before ``ts`` and adapt it to ``intent.extract``.

    COT reports are weekly; treating them as a point-in-time index avoids look-
    ahead. The returned object is a tiny shim with attribute access matching
    what ``intent.extract`` expects (report_date, commercial_net, ...).
    """
    if cot_history is None or cot_history.empty:
        return None
    if not isinstance(cot_history.index, pd.DatetimeIndex):
        return None
    valid = cot_history.loc[cot_history.index <= ts]
    if valid.empty:
        return None
    row = valid.iloc[-1]

    class _COTRow:
        def __init__(self, r: pd.Series, idx: pd.Timestamp):
            self.report_date = str(idx.date())
            self.commercial_net = float(r["commercial_net"]) if "commercial_net" in r else None
            self.commercial_bias = r.get("commercial_bias")
            self.managed_money_net = (
                float(r["managed_money_net"]) if "managed_money_net" in r else None
            )
            self.managed_money_bias = r.get("managed_money_bias")

    return _COTRow(row, valid.index[-1])


def _macro_window(
    macro_series: Optional[Dict[str, pd.DataFrame]],
    ts: pd.Timestamp,
    lookback_days: int = 120,
) -> Optional[Dict[str, pd.DataFrame]]:
    """Trim every macro series to ``[ts - lookback_days, ts]`` to prevent look-ahead."""
    if not macro_series:
        return None
    cutoff_start = ts - pd.Timedelta(days=lookback_days)
    trimmed: Dict[str, pd.DataFrame] = {}
    for name, df in macro_series.items():
        if df is None or df.empty:
            continue
        if not isinstance(df.index, pd.DatetimeIndex):
            continue
        sliced = df.loc[(df.index <= ts) & (df.index >= cutoff_start)]
        if not sliced.empty:
            trimmed[name] = sliced
    return trimmed or None


# ---------------------------------------------------------------------------
# FeatureReplayer
# ---------------------------------------------------------------------------
@dataclass
class _CachedSeries:
    """Pre-computed reusable views to keep per-bar work small."""

    candles_m5: pd.DataFrame
    timeframe_minutes: int
    candles_m15: Optional[pd.DataFrame] = None
    candles_h1: Optional[pd.DataFrame] = None
    macro_series: Optional[Dict[str, pd.DataFrame]] = None
    cot_history: Optional[pd.DataFrame] = None


class FeatureReplayer:
    """Walk candle DataFrames and emit FeaturePack snapshots at each bar close.

    The "current bar" semantics: ``pack_at(ts)`` slices history through and
    including the bar at ``ts``. Indicators are therefore as-of-close, which
    is exactly what a paper-trader running at bar close would see.
    """

    def __init__(
        self,
        candles_m5: pd.DataFrame,
        candles_m15: Optional[pd.DataFrame] = None,
        candles_h1: Optional[pd.DataFrame] = None,
        macro_series: Optional[Dict[str, pd.DataFrame]] = None,
        cot_history: Optional[pd.DataFrame] = None,
        warmup_bars: int = 100,
    ) -> None:
        _ensure_datetime_index(candles_m5, "candles_m5")
        if candles_m15 is not None:
            _ensure_datetime_index(candles_m15, "candles_m15")
        if candles_h1 is not None:
            _ensure_datetime_index(candles_h1, "candles_h1")

        # Sort once so per-bar slicing is monotonic and cheap.
        candles_m5 = candles_m5.sort_index()
        if candles_m15 is not None:
            candles_m15 = candles_m15.sort_index()
        if candles_h1 is not None:
            candles_h1 = candles_h1.sort_index()

        self._data = _CachedSeries(
            candles_m5=candles_m5,
            timeframe_minutes=_infer_timeframe_minutes(candles_m5, default=5),
            candles_m15=candles_m15,
            candles_h1=candles_h1,
            macro_series=macro_series,
            cot_history=cot_history,
        )
        self.warmup_bars = int(warmup_bars)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def iter_packs(
        self,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> Iterator[FeaturePack]:
        """Yield FeaturePacks at each m5 bar in [start, end].

        Warm-up: the first ``warmup_bars`` bars are skipped, since most
        indicators need history. If ``start`` is supplied, it takes precedence
        but is still floored to the warm-up boundary.
        """
        df = self._data.candles_m5
        if df.empty:
            return
        first_valid_pos = min(self.warmup_bars, len(df) - 1)
        first_valid_ts = df.index[first_valid_pos]
        lo = max(start, first_valid_ts) if start is not None else first_valid_ts
        hi = end if end is not None else df.index[-1]

        window = df.loc[(df.index >= lo) & (df.index <= hi)]
        for ts in window.index:
            yield self.pack_at(ts)

    def pack_at(self, ts: pd.Timestamp) -> FeaturePack:
        """Build the FeaturePack as-of close of bar ``ts``."""
        ts = pd.Timestamp(ts)
        m5 = self._slice_through(self._data.candles_m5, ts)
        if m5.empty:
            raise ValueError(f"No m5 history at or before {ts}")

        # Convert to the list-of-dict shape the existing extractors expect.
        candles_list = _df_to_candles(m5)

        flow = flow_feat.extract(candles_list)
        structure = structure_feat.extract(candles_list)

        # Context wants a python datetime so the session classification is exact.
        now_dt = ts.to_pydatetime()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        context = context_feat.extract(candles_list, now=now_dt)

        cot_snap = _cot_snapshot_at(self._data.cot_history, ts)
        intent = intent_feat.extract(cot_snapshot=cot_snap)

        order_flow = order_flow_extract(candles_list)
        vol_regime = vol_regime_extract(
            candles_list, timeframe_minutes=self._data.timeframe_minutes
        )

        # Correlation uses daily price changes, so feed it the m5 dataframe up
        # to ts (it resamples internally). Macro series is windowed for safety.
        macro_window = _macro_window(self._data.macro_series, ts)
        correlation = correlation_extract(m5[["close"]], macro_series=macro_window)

        return FeaturePack(
            timestamp=ts,
            flow=flow,
            structure=structure,
            context=context,
            intent=intent,
            order_flow=order_flow,
            vol_regime=vol_regime,
            correlation=correlation,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _slice_through(df: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
        """Return ``df`` rows with index <= ts. Cheap because df is pre-sorted."""
        return df.loc[df.index <= ts]
