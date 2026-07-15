#!/usr/bin/env python3
"""Run the ATLAS backtest for a named setup.

Two modes, chosen automatically:

  REAL   — if data/candles_<symbol>_<tf>.parquet exists, backtest on it.
           Produces the honest verdict (Sharpe / DSR / PBO / WFE / trades)
           against the promotion gate. THIS is the edge test.

  DRY-RUN — if no data is present, run on deterministic SYNTHETIC candles.
           This proves the full pipeline (features → simulator → metrics →
           promotion gate) works end to end WITHOUT claiming any edge. The
           output is explicitly stamped SYNTHETIC. A green verdict here means
           the plumbing works, NOT that the strategy makes money.

Integrity: the lookahead audit (scripts/audit_lookahead.py) proves the engine
cannot see the future. This runner does not re-implement any timing — it uses
the same FeatureReplayer + EventDrivenSimulator that the audit certified.

Usage:
    python scripts/run_backtest.py --setup htf-retracement-continuation
    python scripts/run_backtest.py --symbol XAUUSD --timeframe 5m
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.cost_model import CostModel
from src.backtest.runner import BacktestRunner
from src.features.replay import FeatureReplayer
from src.strategies.htf_retracement_short import make_strategy


def load_real_candles(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    from src.data_layer.historical.storage import ParquetStore
    store = ParquetStore(root=ROOT / "data")
    name = f"candles_{symbol}_{timeframe}"
    if not store.has(name):
        return None
    df = store.read(name)
    return df if df is not None and not df.empty else None


def synthetic_candles(n: int = 6000, seed: int = 11) -> pd.DataFrame:
    """Deterministic synthetic OHLCV with alternating trend regimes so the
    HTF-retracement-short strategy actually finds setups. Clearly NOT real gold."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="5min", tz="UTC")
    # Piecewise trends: down, up, down, chop — to exercise the strategy in
    # multiple regimes (which the CPCV / walk-forward then splits).
    seg = n // 4
    drift = np.concatenate([
        np.linspace(0, -120, seg),
        np.linspace(-120, -60, seg),
        np.linspace(-60, -200, seg),
        np.linspace(-200, -210, n - 3 * seg),
    ])
    noise = np.cumsum(rng.normal(0, 1.2, n))
    close = 2000 + drift + noise
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = rng.uniform(0.4, 1.8, n)
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    vol = rng.uniform(700, 1600, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def build_feature_provider(candles: pd.DataFrame, warmup: int = 100):
    """Return a callable ts -> feature_pack dict, backed by FeatureReplayer.

    PRECOMPUTES every pack ONCE (via iter_packs) into a dict keyed by ts, then
    serves O(1) lookups. This is correct AND necessary: the runner's walk-
    forward + CPCV call the provider for the same bar many times across folds;
    recomputing per call is O(N^2) and needlessly slow. Precomputing keeps
    every pack as-of-close (no lookahead — pack_at slices index<=ts)."""
    replayer = FeatureReplayer(candles_m5=candles, warmup_bars=warmup)
    packs: Dict[pd.Timestamp, Dict[str, Any]] = {}
    for pack in replayer.iter_packs():
        packs[pd.Timestamp(pack.timestamp)] = pack.to_dict()

    def provider(ts: pd.Timestamp) -> Dict[str, Any]:
        return packs.get(pd.Timestamp(ts), {})

    return provider


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--setup", default="htf-retracement-continuation")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--synthetic-bars", type=int, default=6000)
    args = p.parse_args(argv[1:])

    real = load_real_candles(args.symbol, args.timeframe)
    if real is not None:
        mode = "REAL"
        candles = real
        print(f"[mode] REAL data: {len(candles)} bars "
              f"({candles.index.min()} … {candles.index.max()})")
    else:
        mode = "SYNTHETIC (DRY-RUN)"
        candles = synthetic_candles(n=args.synthetic_bars)
        print("=" * 70)
        print("!! NO REAL DATA FOUND under data/. Running SYNTHETIC DRY-RUN.")
        print("!! A green verdict below proves the PIPELINE works, NOT that the")
        print("!! strategy has edge. Download real data first for the edge test:")
        print("!!   python scripts/download_history.py --start 2019-01-01")
        print("=" * 70)
        print(f"[mode] SYNTHETIC: {len(candles)} bars")

    bar_seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}.get(
        args.timeframe, 300
    )

    runner = BacktestRunner(
        strategy_factory=make_strategy,
        cost_model=CostModel(),
        bar_seconds=bar_seconds,
        # Geometry sized for the data length: IS:OOS 4:1, single-ish step.
        walk_forward_geometry=(max(500, int(len(candles) * 0.6)),
                               max(200, int(len(candles) * 0.15)),
                               max(200, int(len(candles) * 0.15))),
    )

    provider = build_feature_provider(candles)
    hypothesis = {"id": args.setup, "params": {}}

    print(f"[run] evaluating setup '{args.setup}' …")
    verdict = runner.evaluate_hypothesis(
        hypothesis_yaml=hypothesis,
        candles=candles,
        feature_pack_provider=provider,
    )

    print("\n" + "=" * 70)
    print(f"BACKTEST VERDICT  [{mode}]  setup={args.setup}")
    print("=" * 70)
    _print_verdict(verdict)
    print("=" * 70)
    if mode.startswith("SYNTHETIC"):
        print("Reminder: SYNTHETIC result. Not evidence of edge. Get real data.")
    else:
        gate = verdict.get("verdict", "?")
        if gate == "PASS":
            print("PASS on real data → promote to SHADOW (paper) per the plan.")
        else:
            print("FAIL on real data → this raw pattern lacks edge at these params.")
            print("Next: parameter search (logged as trials for DSR) or shelve.")
    print("=" * 70)
    return 0


def _print_verdict(v: Dict[str, Any]) -> None:
    def g(k, default="—"):
        return v.get(k, default)

    print(f"  verdict:        {g('verdict')}")
    is_ = v.get("in_sample", {}) or {}
    print(f"  in-sample:      sharpe={is_.get('sharpe', '—')}  "
          f"trades={is_.get('trades', '—')}  win_rate={is_.get('win_rate', '—')}")
    print(f"  DSR:            {g('dsr')}   (gate: > 0.95)")
    print(f"  PBO:            {g('pbo')}   (gate: < 0.20)")
    wf = v.get("walk_forward", {}) or {}
    print(f"  walk-forward:   wfe_pass_ratio={wf.get('wfe_pass_ratio', '—')}  (gate: >= 0.70)")
    print(f"  OOS trades:     {g('oos_trades', is_.get('trades', '—'))}   (gate: >= 200)")
    if v.get("fail_reasons"):
        print("  fail reasons:")
        for r in v["fail_reasons"]:
            print(f"    - {r}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
