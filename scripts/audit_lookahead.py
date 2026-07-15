#!/usr/bin/env python3
"""Lookahead-bias / data-poisoning audit for the ATLAS backtest engine.

THE TEST (gold standard for proving no lookahead)
--------------------------------------------------
1. Build a candle series. Run the full pipeline (FeatureReplayer + simulator +
   a deterministic strategy). Record every FeaturePack and every trade.
2. POISON every bar AFTER a cut index `k` with absurd values (10x spikes,
   inverted highs/lows, exploded volume).
3. Re-run on the poisoned series.
4. ASSERT:
   (a) FeaturePack at every timestamp <= cut is byte-identical between the
       clean and poisoned runs. If a future bar can change a past feature,
       a lookahead leak exists in the feature layer.
   (b) Every trade that ENTERED at or before the cut is identical (entry time,
       price, direction, size). If poisoning the future changes a past fill or
       exit, the simulator peeks.
   (c) The simulator fills orders on the NEXT bar's open, never the signal
       bar's close (verified directly).

If all three hold, the engine cannot see the future. Period.

Run:  python scripts/audit_lookahead.py
Exit: 0 = clean, 1 = LEAK DETECTED.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.cost_model import CostModel
from src.backtest.simulator import EventDrivenSimulator
from src.backtest.strategy import Bar, Order, PositionState, Strategy
from src.features.replay import FeatureReplayer


# ---------------------------------------------------------------------------
# A deterministic strategy that reacts to structure so poisoning would matter
# if the engine leaked. It shorts after two consecutive lower closes, targeting
# a fixed R, stop above recent high — a toy version of the operator's edge.
# ---------------------------------------------------------------------------
class TwoBarMomentumShort(Strategy):
    def __init__(self) -> None:
        self._closes: List[float] = []
        self._highs: List[float] = []

    def on_bar(self, bar: Bar, feature_pack: Dict[str, Any], position: PositionState) -> Optional[Order]:
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        if position.is_open or len(self._closes) < 3:
            return None
        # Two consecutive lower closes -> short.
        if self._closes[-1] < self._closes[-2] < self._closes[-3]:
            recent_high = max(self._highs[-3:])
            entry_ref = bar.close
            stop = recent_high * 1.001
            risk = stop - entry_ref
            tp = entry_ref - 2.0 * risk  # 2R target
            return Order(
                direction="short",
                entry_price=entry_ref,
                stop_loss=stop,
                take_profits=[tp],
                size=1.0,
            )
        return None


def build_candles(n: int = 400, seed: int = 7) -> pd.DataFrame:
    """Deterministic synthetic OHLCV with trend + noise. No randomness at runtime
    beyond the seeded generator so the audit is reproducible."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    # Gentle downtrend with mean-reverting noise so the strategy actually trades.
    drift = np.linspace(0, -40, n)
    noise = np.cumsum(rng.normal(0, 1.5, n))
    close = 2000 + drift + noise
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = rng.uniform(0.5, 2.0, n)
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = rng.uniform(800, 1500, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def poison_future(df: pd.DataFrame, cut: int) -> pd.DataFrame:
    """Return a copy of df where every bar AFTER `cut` is corrupted with garbage."""
    poisoned = df.copy()
    tail = poisoned.iloc[cut + 1 :]
    if tail.empty:
        return poisoned
    # 10x spikes, inverted structure, exploded volume — maximally different.
    poisoned.iloc[cut + 1 :, poisoned.columns.get_loc("open")] = tail["open"].to_numpy() * 10.0
    poisoned.iloc[cut + 1 :, poisoned.columns.get_loc("high")] = tail["high"].to_numpy() * 12.0
    poisoned.iloc[cut + 1 :, poisoned.columns.get_loc("low")] = tail["low"].to_numpy() * 0.1
    poisoned.iloc[cut + 1 :, poisoned.columns.get_loc("close")] = tail["close"].to_numpy() * 8.0
    poisoned.iloc[cut + 1 :, poisoned.columns.get_loc("volume")] = tail["volume"].to_numpy() * 100.0
    return poisoned


def run_pipeline(candles: pd.DataFrame, warmup: int = 50):
    """Return (packs_by_ts, trades_df) for a full replay + simulate run."""
    replayer = FeatureReplayer(candles_m5=candles, warmup_bars=warmup)
    packs = list(replayer.iter_packs())
    packs_by_ts = {str(p.timestamp): p.to_dict() for p in packs}

    # The simulator consumes one feature pack per bar in the sliced range.
    # We run over the same [warmup .. end] window the packs cover.
    first_ts = pd.Timestamp(packs[0].timestamp) if packs else candles.index[0]
    cost = CostModel()
    sim = EventDrivenSimulator(candles=candles, cost_model=cost, bar_seconds=300)
    result = sim.run(
        TwoBarMomentumShort(),
        feature_pack_iter=[p.to_dict() for p in packs],
        start=first_ts,
    )
    return packs_by_ts, result.trades


def audit() -> int:
    print("=" * 70)
    print("ATLAS LOOKAHEAD / POISONING AUDIT")
    print("=" * 70)

    candles = build_candles(n=400)
    cut = 250
    cut_ts = candles.index[cut]
    print(f"Candles: {len(candles)} bars. Cut at index {cut} ({cut_ts}).")
    print(f"Poisoning every bar AFTER the cut with 8–100x garbage.\n")

    clean_packs, clean_trades = run_pipeline(candles)
    poisoned_candles = poison_future(candles, cut)
    pois_packs, pois_trades = run_pipeline(poisoned_candles)

    failures: List[str] = []

    # ---- (a) feature packs at/before the cut must be byte-identical ----
    checked = 0
    for ts_str, clean_pack in clean_packs.items():
        ts = pd.Timestamp(ts_str)
        if ts > cut_ts:
            continue
        checked += 1
        pois_pack = pois_packs.get(ts_str)
        if pois_pack is None:
            failures.append(f"[FEATURE] pack missing at {ts_str} in poisoned run")
            continue
        if clean_pack != pois_pack:
            # find the first differing lens for a helpful message
            for lens in clean_pack:
                if clean_pack[lens] != pois_pack.get(lens):
                    failures.append(
                        f"[FEATURE LEAK] {ts_str} lens '{lens}' changed when FUTURE bars were poisoned"
                    )
                    break
    print(f"(a) Feature packs at/before cut checked: {checked}. "
          f"{'OK' if not failures else 'LEAKS FOUND'}")

    # ---- (b) trades entered at/before the cut must be identical ----
    def pre_cut_trades(trades: pd.DataFrame) -> pd.DataFrame:
        if trades.empty:
            return trades
        return trades[trades["entry_time"] <= cut_ts].reset_index(drop=True)

    ct = pre_cut_trades(clean_trades)
    pt = pre_cut_trades(pois_trades)
    if len(ct) != len(pt):
        failures.append(
            f"[TRADE LEAK] pre-cut trade COUNT differs: clean={len(ct)} poisoned={len(pt)}"
        )
    else:
        cols = ["entry_time", "direction", "entry_price", "size"]
        for i in range(len(ct)):
            for c in cols:
                cv, pv = ct.iloc[i][c], pt.iloc[i][c]
                same = (cv == pv) or (isinstance(cv, float) and abs(cv - pv) < 1e-9)
                if not same:
                    failures.append(
                        f"[TRADE LEAK] pre-cut trade {i} field '{c}' differs: {cv} vs {pv}"
                    )
    # Note: a trade that ENTERED pre-cut but EXITS post-cut may legitimately
    # exit differently (the poisoned future genuinely changes what price did
    # after the cut). We therefore only assert on ENTRY identity, which is the
    # decision the engine made with pre-cut information.
    print(f"(b) Pre-cut trades: clean={len(ct)} poisoned={len(pt)}. "
          f"Entry identity {'OK' if not any('TRADE LEAK' in f for f in failures) else 'LEAKS FOUND'}")

    # ---- (c) fills happen at NEXT bar open, never signal-bar close ----
    fill_check = _verify_next_bar_fill()
    if fill_check is not None:
        failures.append(fill_check)
    print(f"(c) Next-bar-open fill rule: {'OK' if fill_check is None else 'VIOLATED'}")

    print("\n" + "=" * 70)
    if failures:
        print(f"RESULT: {len(failures)} LEAK(S) DETECTED — engine is POLLUTED.")
        for f in failures:
            print("  - " + f)
        print("=" * 70)
        return 1
    print("RESULT: CLEAN. No future information reaches past features or past")
    print("decisions. Fills occur strictly on the next bar's open.")
    print("=" * 70)
    return 0


def _verify_next_bar_fill() -> Optional[str]:
    """Directly verify: a signal on bar i fills at bar i+1's OPEN, not bar i's close."""
    idx = pd.date_range("2024-01-01", periods=5, freq="5min", tz="UTC")
    # Distinct opens so we can tell which bar the fill used.
    candles = pd.DataFrame(
        {
            "open":  [100.0, 101.0, 102.0, 103.0, 104.0],
            "high":  [100.5, 101.5, 102.5, 103.5, 104.5],
            "low":   [ 99.5, 100.5, 101.5, 102.5, 103.5],
            "close": [100.2, 101.2, 102.2, 103.2, 104.2],
            "volume":[1000, 1000, 1000, 1000, 1000],
        },
        index=idx,
    )

    class ShortOnBar1(Strategy):
        def __init__(self) -> None:
            self.i = 0
        def on_bar(self, bar, fp, pos):
            self.i += 1
            if self.i == 2 and not pos.is_open:  # signal on the 2nd bar (index 1)
                return Order(
                    direction="short",
                    entry_price=bar.close,
                    stop_loss=200.0,
                    take_profits=[50.0],
                    size=1.0,
                )
            return None

    cost = CostModel()
    sim = EventDrivenSimulator(candles=candles, cost_model=cost, bar_seconds=300)
    res = sim.run(ShortOnBar1(), feature_pack_iter=[{} for _ in range(len(candles))])
    if res.trades.empty:
        return "[FILL] no trade produced — cannot verify fill timing"
    entry_time = pd.Timestamp(res.trades.iloc[0]["entry_time"])
    # Signal was on bar index 1 (ts idx[1]); fill MUST be on bar index 2 (idx[2]).
    if entry_time != idx[2]:
        return (
            f"[FILL LEAK] signal on bar idx1 filled at {entry_time}, "
            f"expected next bar open at {idx[2]}"
        )
    # And the entry price must derive from bar idx2's OPEN (102.0), not idx1 close (101.2).
    entry_price = float(res.trades.iloc[0]["entry_price"])
    # short entry = open - latency_drift, then minus spread bps → should be ~ <= 102.0 and clearly not ~101.2
    if abs(entry_price - 101.2) < 0.3:
        return f"[FILL LEAK] entry price {entry_price} looks like signal-bar close, not next-bar open"
    return None


if __name__ == "__main__":
    sys.exit(audit())
