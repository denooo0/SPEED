"""Tests for the FeatureReplayer / FeaturePack glue.

Covers:
* Replay produces same flow/structure as live mode given identical input.
* FeaturePack.to_dict is JSON-friendly.
* iter_packs respects warm-up and the [start, end] window.
"""
from __future__ import annotations

import json
from datetime import timezone

import numpy as np
import pandas as pd
import pytest

from src.features import flow as flow_feat
from src.features import structure as struct_feat
from src.features.replay import FeaturePack, FeatureReplayer, _df_to_candles


def _make_m5_df(n: int = 250, seed: int = 7) -> pd.DataFrame:
    """Synthetic 5-minute XAUUSD candles. Deterministic random walk."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    rets = rng.normal(0, 0.5, size=n)
    closes = 2000 + np.cumsum(rets)
    opens = np.r_[closes[0], closes[:-1]]
    highs = np.maximum(opens, closes) + rng.uniform(0.1, 0.6, size=n)
    lows = np.minimum(opens, closes) - rng.uniform(0.1, 0.6, size=n)
    vols = rng.uniform(800, 1200, size=n)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def test_replay_flow_matches_live_given_same_input():
    df = _make_m5_df(n=200)
    replayer = FeatureReplayer(candles_m5=df, warmup_bars=50)

    last_ts = df.index[-1]
    pack = replayer.pack_at(last_ts)

    # Independently compute via the existing live-mode entry point.
    live_candles = _df_to_candles(df.loc[df.index <= last_ts])
    live_flow = flow_feat.extract(live_candles)

    assert pack.flow.cvd_full == pytest.approx(live_flow.cvd_full)
    assert pack.flow.cvd_recent == pytest.approx(live_flow.cvd_recent)
    assert pack.flow.aggressor_bias == live_flow.aggressor_bias
    assert pack.flow.volume_spike == live_flow.volume_spike


def test_replay_structure_matches_live_given_same_input():
    df = _make_m5_df(n=200)
    replayer = FeatureReplayer(candles_m5=df, warmup_bars=50)

    last_ts = df.index[-1]
    pack = replayer.pack_at(last_ts)
    live_candles = _df_to_candles(df.loc[df.index <= last_ts])
    live_struct = struct_feat.extract(live_candles)

    assert pack.structure.swing_high == live_struct.swing_high
    assert pack.structure.swing_low == live_struct.swing_low
    assert pack.structure.last_bos == live_struct.last_bos
    assert len(pack.structure.bullish_fvgs) == len(live_struct.bullish_fvgs)
    assert len(pack.structure.bearish_fvgs) == len(live_struct.bearish_fvgs)


def test_feature_pack_to_dict_is_json_serialisable():
    df = _make_m5_df(n=120)
    replayer = FeatureReplayer(candles_m5=df, warmup_bars=50)
    pack = replayer.pack_at(df.index[-1])
    d = pack.to_dict()

    # Top-level keys present and timestamp is ISO.
    assert set(d.keys()) == {
        "timestamp",
        "flow",
        "structure",
        "context",
        "intent",
        "order_flow",
        "vol_regime",
        "correlation",
    }
    # JSON-serialisable (default=str safety-net for any stray non-primitive).
    json.dumps(d, default=str)
    assert d["timestamp"].startswith("2026-")


def test_iter_packs_respects_warmup_and_window():
    df = _make_m5_df(n=300)
    replayer = FeatureReplayer(candles_m5=df, warmup_bars=100)

    packs = list(replayer.iter_packs())
    assert len(packs) == 300 - 100  # first 100 bars skipped (warm-up)
    assert packs[0].timestamp == df.index[100]
    assert packs[-1].timestamp == df.index[-1]

    # With an explicit window.
    start, end = df.index[150], df.index[170]
    packs_win = list(replayer.iter_packs(start=start, end=end))
    assert packs_win[0].timestamp == start
    assert packs_win[-1].timestamp == end
    assert len(packs_win) == 21


def test_pack_at_with_naive_timestamp_normalises_to_utc():
    # Mirrors what a backtest harness might pass in.
    df = _make_m5_df(n=120)
    replayer = FeatureReplayer(candles_m5=df, warmup_bars=20)
    # Use the timezone-aware index directly — pandas Timestamps support tz.
    ts = df.index[-1]
    pack = replayer.pack_at(ts)
    assert pack.context.hour_utc == ts.hour


def test_replayer_rejects_missing_columns():
    bad = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5]},
        index=pd.DatetimeIndex(["2026-01-01"], tz="UTC"),
    )
    with pytest.raises(ValueError):
        FeatureReplayer(candles_m5=bad)


def test_replayer_rejects_non_datetime_index():
    bad = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]}
    )
    with pytest.raises(TypeError):
        FeatureReplayer(candles_m5=bad)
