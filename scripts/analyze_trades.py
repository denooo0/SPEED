#!/usr/bin/env python3
"""Mine the backtest trades to find which confluences separate winners from losers.

The naive HtfRetracementShort took 1,132 trades at 32% win rate — 1pp below
breakeven. The edge (if any) is in SELECTION. This script computes, at each
trade's entry, a battery of candidate confluence features, then measures which
ones separate winning trades from losing ones. Those separators are the
empirically-found confluences to build the filtered strategy from.

No lookahead: every feature at a trade's entry uses only candles up to the
signal bar (the bar before the fill).

Run:  python scripts/analyze_trades.py --timeframe 1h
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.cost_model import CostModel
from src.backtest.simulator import EventDrivenSimulator
from src.data_layer.historical.storage import ParquetStore
from src.strategies.htf_retracement_short import make_strategy


def compute_context(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized candidate confluence features on the full candle frame.

    All features are causal (use only past/current bar), so looking one up at a
    trade's entry timestamp introduces no lookahead."""
    c = df["close"]
    out = pd.DataFrame(index=df.index)
    out["ma50"] = c.rolling(50).mean()
    out["ma200"] = c.rolling(200).mean()
    # Trend strength: slope of MA50 over the last 20 bars, normalized to bp.
    out["ma50_slope_bp"] = (out["ma50"] - out["ma50"].shift(20)) / out["ma50"] * 10_000
    # How deep the retracement got: distance of close from MA50 at entry (bp).
    out["dist_ma50_bp"] = (c - out["ma50"]) / out["ma50"] * 10_000
    # Secular filter: is price below the slow MA? (short-friendly regime)
    out["below_ma200"] = (c < out["ma200"]).astype(int)
    # Volatility regime: ATR% (14).
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - c.shift()).abs(),
        (df["low"] - c.shift()).abs(),
    ], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    out["atr_pct"] = out["atr14"] / c * 10_000  # bp
    # Recent momentum: 20-bar return (bp). Strongly negative = strong downmove.
    out["ret20_bp"] = c.pct_change(20) * 10_000
    # Session / time.
    out["hour"] = df.index.hour
    out["dow"] = df.index.dayofweek
    # London (7-16 UTC) / NY (13-22 UTC) killzone flags.
    out["london"] = ((out["hour"] >= 7) & (out["hour"] < 16)).astype(int)
    out["ny"] = ((out["hour"] >= 13) & (out["hour"] < 22)).astype(int)
    out["ny_overlap"] = ((out["hour"] >= 13) & (out["hour"] < 16)).astype(int)
    return out


def get_trades(df: pd.DataFrame, bar_seconds: int) -> pd.DataFrame:
    sim = EventDrivenSimulator(candles=df, cost_model=CostModel(), bar_seconds=bar_seconds)
    result = sim.run(make_strategy({"id": "analyze"}), feature_pack_iter=[{} for _ in range(len(df))])
    return result.trades


def asof_context(context: pd.DataFrame, entry_time: pd.Timestamp) -> pd.Series:
    """Context as of the last bar at or before entry (causal)."""
    sub = context.loc[context.index <= entry_time]
    if sub.empty:
        return pd.Series(dtype=float)
    return sub.iloc[-1]


def analyze(df: pd.DataFrame, trades: pd.DataFrame) -> None:
    context = compute_context(df)
    rows: List[Dict[str, Any]] = []
    for _, t in trades.iterrows():
        ctx = asof_context(context, pd.Timestamp(t["entry_time"]))
        if ctx.empty:
            continue
        row = ctx.to_dict()
        row["r_multiple"] = float(t["r_multiple"])
        row["win"] = 1 if float(t["r_multiple"]) > 0 else 0
        row["exit_reason"] = t["exit_reason"]
        rows.append(row)
    d = pd.DataFrame(rows).dropna()
    n = len(d)
    base_wr = d["win"].mean()
    print(f"\nAnalyzed {n} trades. Baseline win rate: {base_wr:.1%}\n")

    numeric = ["ma50_slope_bp", "dist_ma50_bp", "atr_pct", "ret20_bp"]
    print("=" * 74)
    print("WINNER vs LOSER — numeric features (median)")
    print("=" * 74)
    print(f"{'feature':<16}{'winners':>14}{'losers':>14}{'separation':>14}")
    win = d[d["win"] == 1]
    los = d[d["win"] == 0]
    for f in numeric:
        wv, lv = win[f].median(), los[f].median()
        spread = win[f].std() + los[f].std()
        sep = (wv - lv) / spread if spread else 0.0
        print(f"{f:<16}{wv:>14.1f}{lv:>14.1f}{sep:>14.2f}")

    print("\n" + "=" * 74)
    print("CONDITIONAL WIN RATE — does a confluence lift win rate above 33.3%?")
    print("(33.3% = breakeven at 2R; baseline is 32.2%)")
    print("=" * 74)
    conditions = {
        "below_ma200 (secular short regime)": d["below_ma200"] == 1,
        "strong downtrend (ma50_slope < -50bp)": d["ma50_slope_bp"] < -50,
        "very strong down (ma50_slope < -100bp)": d["ma50_slope_bp"] < -100,
        "deep retrace (dist_ma50 > +20bp above MA)": d["dist_ma50_bp"] > 20,
        "high vol (atr_pct > median)": d["atr_pct"] > d["atr_pct"].median(),
        "low vol (atr_pct < median)": d["atr_pct"] < d["atr_pct"].median(),
        "strong recent down (ret20 < -100bp)": d["ret20_bp"] < -100,
        "London session": d["london"] == 1,
        "NY session": d["ny"] == 1,
        "NY-London overlap": d["ny_overlap"] == 1,
        "off-session (not London/NY)": (d["london"] == 0) & (d["ny"] == 0),
    }
    print(f"{'confluence':<44}{'trades':>8}{'win%':>8}{'exp(R)':>8}")
    results = []
    for label, mask in conditions.items():
        sub = d[mask]
        if len(sub) < 20:
            continue
        wr = sub["win"].mean()
        exp = wr * 2.0 - (1 - wr) * 1.0
        flag = "  <<<" if wr > 0.333 else ""
        results.append((label, len(sub), wr, exp, flag))
        print(f"{label:<44}{len(sub):>8}{wr*100:>7.1f}%{exp:>+8.2f}{flag}")

    # Best single confluence + a stacked combo.
    print("\n" + "=" * 74)
    print("STACKED CONFLUENCE — combine the best separators")
    print("=" * 74)
    combos = {
        "below_ma200 AND strong-down(slope<-50)":
            (d["below_ma200"] == 1) & (d["ma50_slope_bp"] < -50),
        "below_ma200 AND strong-down AND London":
            (d["below_ma200"] == 1) & (d["ma50_slope_bp"] < -50) & (d["london"] == 1),
        "very-strong-down AND ret20<-100":
            (d["ma50_slope_bp"] < -100) & (d["ret20_bp"] < -100),
        "below_ma200 AND deep-retrace(dist>20)":
            (d["below_ma200"] == 1) & (d["dist_ma50_bp"] > 20),
    }
    print(f"{'combo':<48}{'trades':>8}{'win%':>8}{'exp(R)':>8}")
    for label, mask in combos.items():
        sub = d[mask]
        if len(sub) < 15:
            print(f"{label:<48}{len(sub):>8}{'n<15':>8}")
            continue
        wr = sub["win"].mean()
        exp = wr * 2.0 - (1 - wr) * 1.0
        flag = "  <<<" if wr > 0.333 else ""
        print(f"{label:<48}{len(sub):>8}{wr*100:>7.1f}%{exp:>+8.2f}{flag}")

    print("\n" + "=" * 74)
    print("READING: any row flagged <<< beats the 2R breakeven on win rate.")
    print("Those are the confluences to encode into the filtered strategy.")
    print("Fewer trades + higher win% = the quality-over-quantity the operator wants.")
    print("=" * 74)


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--timeframe", default="1h")
    args = p.parse_args(argv[1:])

    store = ParquetStore(root=ROOT / "data")
    name = f"candles_{args.symbol}_{args.timeframe}"
    if not store.has(name):
        print(f"No data at data/{name}.parquet. Run download/ingest first.")
        return 1
    df = store.read(name)
    bar_seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}.get(args.timeframe, 3600)
    trades = get_trades(df, bar_seconds)
    print(f"Backtest produced {len(trades)} trades on {name} ({len(df)} bars).")
    analyze(df, trades)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
