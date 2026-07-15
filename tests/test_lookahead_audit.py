"""Permanent lookahead-bias regression test.

Wraps scripts/audit_lookahead.py so the poisoning audit runs in the full
suite. If anyone ever introduces a future-peeking change to the feature
replayer or simulator, this test fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_lookahead import (
    TwoBarMomentumShort,
    build_candles,
    poison_future,
    run_pipeline,
    _verify_next_bar_fill,
)


def test_future_poisoning_does_not_change_past_features():
    candles = build_candles(n=400)
    cut = 250
    cut_ts = candles.index[cut]
    clean_packs, _ = run_pipeline(candles)
    pois_packs, _ = run_pipeline(poison_future(candles, cut))

    checked = 0
    for ts_str, clean_pack in clean_packs.items():
        if pd.Timestamp(ts_str) > cut_ts:
            continue
        checked += 1
        assert ts_str in pois_packs, f"pack vanished at {ts_str} under poisoning"
        assert clean_pack == pois_packs[ts_str], (
            f"FEATURE LEAK at {ts_str}: future poisoning changed a past feature pack"
        )
    assert checked > 50, "audit should check a meaningful number of pre-cut bars"


def test_future_poisoning_does_not_change_past_trade_entries():
    candles = build_candles(n=400)
    cut = 250
    cut_ts = candles.index[cut]
    _, clean_trades = run_pipeline(candles)
    _, pois_trades = run_pipeline(poison_future(candles, cut))

    def pre_cut(t: pd.DataFrame) -> pd.DataFrame:
        return t[t["entry_time"] <= cut_ts].reset_index(drop=True) if not t.empty else t

    ct, pt = pre_cut(clean_trades), pre_cut(pois_trades)
    assert len(ct) == len(pt), f"pre-cut trade count changed: {len(ct)} vs {len(pt)}"
    for i in range(len(ct)):
        for col in ("entry_time", "direction", "size"):
            assert ct.iloc[i][col] == pt.iloc[i][col], f"TRADE LEAK field {col} @ {i}"
        assert abs(float(ct.iloc[i]["entry_price"]) - float(pt.iloc[i]["entry_price"])) < 1e-9, (
            f"TRADE LEAK entry_price @ {i}"
        )


def test_orders_fill_on_next_bar_open_not_signal_close():
    assert _verify_next_bar_fill() is None, "simulator violated next-bar-open fill rule"
