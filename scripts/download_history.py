#!/usr/bin/env python3
"""Download historical XAUUSD (+ macro + COT) for the ATLAS backtest.

WHY THIS EXISTS
---------------
The ATLAS build sandbox's network policy blocks external data hosts
(api.bybit.com, yfinance, stooq — all 403 at the gateway). So historical
data must be pulled from an environment WITH internet access:
  - the operator's own machine, or
  - a session whose network policy permits the data hosts.

This script pulls the data, validates it, and writes parquet files under
data/. Once committed (or copied into the sandbox), the backtest runs fully
offline — no network needed for the analysis itself.

WHAT IT PULLS
-------------
  XAUUSD OHLCV  (Bybit via the existing BybitHistoricalFetcher)
  Macro series  (DXY, US10Y, VIX, WTI via FREDMacro — FRED key or yfinance)
  COT archive   (CFTC weekly, via COTArchive)

USAGE
-----
    pip install -r requirements.txt
    python scripts/download_history.py --start 2019-01-01 --timeframes 5m 15m 1h

    # macro + COT too
    python scripts/download_history.py --start 2019-01-01 --macro --cot

    # Bybit gold symbol varies; override if needed
    python scripts/download_history.py --symbol XAUTUSDT

NOTE ON GOLD SYMBOL
-------------------
Bybit lists tokenised/paxg gold under symbols like XAUTUSDT, not "XAUUSD".
Run once and check the printed available-symbol hint if the fetch is empty.
For pure spot XAUUSD, a broker/MT5 export or a FRED/LBMA daily series may be
the better source — this script also accepts a CSV via --from-csv.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _store():
    from src.data_layer.historical.storage import ParquetStore
    return ParquetStore(root=ROOT / "data")


def download_candles(symbol: str, timeframes: List[str], start: str, end: Optional[str]) -> None:
    from src.data_layer.bybit_connector import BybitConnector
    from src.data_layer.historical.bybit_historical import BybitHistoricalFetcher
    import pandas as pd

    connector = BybitConnector(api_key="", api_secret="", testnet=False)
    fetcher = BybitHistoricalFetcher(connector)
    store = _store()

    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp.utcnow().tz_localize("UTC")

    for tf in timeframes:
        print(f"[candles] {symbol} {tf}  {start_ts.date()} → {end_ts.date()} …")
        try:
            df = fetcher.fetch_candles_range(symbol=symbol, timeframe=tf, start=start_ts, end=end_ts)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {type(e).__name__}: {str(e)[:160]}")
            print("  Hint: gold on Bybit is often 'XAUTUSDT'. Try --symbol XAUTUSDT,")
            print("        or export candles from MT5/your broker and use --from-csv.")
            continue
        if df is None or df.empty:
            print("  EMPTY — check symbol / timeframe availability.")
            continue
        name = f"candles_{symbol}_{tf}"
        store.write(name, df)
        print(f"  wrote {len(df)} rows → data/{name}.parquet "
              f"({df.index.min()} … {df.index.max()})")


def download_macro(start: str) -> None:
    from src.data_layer.historical.fred_macro import FREDMacro
    import pandas as pd

    macro = FREDMacro()
    store = _store()
    series = {"DXY": "DTWEXBGS", "US10Y": "DGS10", "VIX": "VIXCLS", "WTI": "DCOILWTICO"}
    start_ts = pd.Timestamp(start, tz="UTC")
    for label, code in series.items():
        print(f"[macro] {label} ({code}) …")
        try:
            df = macro.fetch_series(code, start=start_ts, end=pd.Timestamp.utcnow().tz_localize('UTC'))
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {type(e).__name__}: {str(e)[:120]}")
            continue
        if df is None or df.empty:
            print("  EMPTY")
            continue
        store.write(f"macro_{label}", df)
        print(f"  wrote {len(df)} rows → data/macro_{label}.parquet")


def download_cot(start: str) -> None:
    from src.data_layer.historical.cot_archive import COTArchive
    import pandas as pd

    archive = COTArchive()
    store = _store()
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp.utcnow().tz_localize("UTC")
    print(f"[cot] CFTC gold  {start_ts.year} → {end_ts.year} …")
    try:
        df = archive.fetch_history(start=start_ts, end=end_ts)
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED: {type(e).__name__}: {str(e)[:120]}")
        return
    if df is None or df.empty:
        print("  EMPTY")
        return
    store.write("cot_gold", df)
    print(f"  wrote {len(df)} rows → data/cot_gold.parquet")


def ingest_csv(csv_path: str, symbol: str, timeframe: str) -> None:
    """Load a broker/MT5 CSV export → parquet. Expected columns:
    time/date, open, high, low, close, volume (tickvol acceptable)."""
    import pandas as pd

    store = _store()
    print(f"[csv] ingesting {csv_path} …")
    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]
    time_col = next((c for c in ("time", "date", "datetime", "timestamp") if c in df.columns), None)
    if time_col is None:
        print("  FAILED: no time/date column found")
        return
    df[time_col] = pd.to_datetime(df[time_col], utc=True, errors="coerce")
    df = df.dropna(subset=[time_col]).set_index(time_col).sort_index()
    vol_col = next((c for c in ("volume", "tickvol", "vol") if c in df.columns), None)
    keep = {"open", "high", "low", "close"}
    if not keep.issubset(df.columns):
        print(f"  FAILED: CSV missing OHLC columns (have {list(df.columns)})")
        return
    out = df[["open", "high", "low", "close"]].copy()
    out["volume"] = df[vol_col] if vol_col else 0.0
    name = f"candles_{symbol}_{timeframe}"
    store.write(name, out)
    print(f"  wrote {len(out)} rows → data/{name}.parquet ({out.index.min()} … {out.index.max()})")


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--timeframes", nargs="+", default=["5m", "15m", "1h"])
    p.add_argument("--start", default="2019-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--macro", action="store_true", help="also pull FRED macro series")
    p.add_argument("--cot", action="store_true", help="also pull CFTC COT archive")
    p.add_argument("--from-csv", default=None, help="ingest a broker/MT5 CSV instead of Bybit")
    p.add_argument("--csv-timeframe", default="5m", help="timeframe label for --from-csv")
    args = p.parse_args(argv[1:])

    (ROOT / "data").mkdir(parents=True, exist_ok=True)

    if args.from_csv:
        ingest_csv(args.from_csv, args.symbol, args.csv_timeframe)
    else:
        download_candles(args.symbol, args.timeframes, args.start, args.end)
    if args.macro:
        download_macro(args.start)
    if args.cot:
        download_cot(args.start)

    print("\nDone. Backtest reads data/*.parquet offline:")
    print("  python scripts/run_backtest.py --setup htf-retracement-continuation")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
