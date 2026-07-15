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
    """Load a broker/MT5 CSV export → parquet.

    Handles the common export shapes automatically:
      * MetaTrader 5 "Export": TAB-separated, headers like
        <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>
        (angle brackets stripped; separate DATE + TIME columns combined).
      * MT4-style: DATE,TIME,OPEN,HIGH,LOW,CLOSE,VOLUME (comma or tab).
      * Generic: a single datetime/time/date column + OHLC(+volume).
    Separator is auto-detected (tab or comma).
    """
    import pandas as pd

    store = _store()
    print(f"[csv] ingesting {csv_path} …")

    # Auto-detect separator (tab vs comma). engine='python' enables sep=None sniff.
    try:
        df = pd.read_csv(csv_path, sep=None, engine="python")
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED to read CSV: {type(e).__name__}: {str(e)[:120]}")
        return

    # Normalise headers: lowercase, strip angle brackets/whitespace (MT5 uses <DATE> etc).
    df.columns = [str(c).strip().strip("<>").lower() for c in df.columns]

    # Build a datetime index. Prefer a combined datetime, else DATE+TIME, else a
    # single date/time column.
    dt = None
    if "date" in df.columns and "time" in df.columns:
        dt = pd.to_datetime(
            df["date"].astype(str) + " " + df["time"].astype(str),
            utc=True, errors="coerce", format="mixed",
        )
    else:
        for c in ("datetime", "timestamp", "date", "time"):
            if c in df.columns:
                dt = pd.to_datetime(df[c], utc=True, errors="coerce")
                break
    if dt is None or dt.isna().all():
        print(f"  FAILED: could not parse a datetime from columns {list(df.columns)}")
        print("  MT5 tip: File → export gives <DATE> <TIME> columns — this handles that.")
        return

    df = df.assign(_dt=dt).dropna(subset=["_dt"]).set_index("_dt").sort_index()
    df.index.name = "timestamp"

    keep = {"open", "high", "low", "close"}
    if not keep.issubset(df.columns):
        print(f"  FAILED: CSV missing OHLC columns (have {list(df.columns)})")
        return

    out = df[["open", "high", "low", "close"]].astype(float).copy()
    vol_col = next((c for c in ("volume", "tickvol", "vol", "real_volume") if c in df.columns), None)
    out["volume"] = df[vol_col].astype(float) if vol_col else 0.0
    # Drop exact-duplicate timestamps (MT5 exports can repeat the last bar).
    out = out[~out.index.duplicated(keep="last")]

    name = f"candles_{symbol}_{timeframe}"
    store.write(name, out)
    print(f"  wrote {len(out)} rows → data/{name}.parquet ({out.index.min()} … {out.index.max()})")


_TF_ALIASES = {
    "m1": "1m", "1m": "1m", "m5": "5m", "5m": "5m", "m15": "15m", "15m": "15m",
    "m30": "30m", "30m": "30m", "h1": "1h", "1h": "1h", "h4": "4h", "4h": "4h",
    "d1": "1d", "1d": "1d", "daily": "1d",
}


def _infer_from_filename(path: Path) -> tuple:
    """Guess (symbol, timeframe) from a filename like XAUUSD_M5.csv or gold-15m.csv."""
    stem = path.stem.lower()
    tf = "5m"
    for token in stem.replace("-", "_").split("_"):
        if token in _TF_ALIASES:
            tf = _TF_ALIASES[token]
            break
    symbol = "XAUUSD"
    if "xau" in stem or "gold" in stem:
        symbol = "XAUUSD"
    return symbol, tf


def ingest_inbox() -> None:
    """Ingest every CSV dropped in inbox/ (uploaded via GitHub web, no local dev)."""
    inbox = ROOT / "inbox"
    csvs = sorted(inbox.glob("*.csv")) + sorted(inbox.glob("*.CSV"))
    if not csvs:
        print("[inbox] no CSV files in inbox/. Upload MT5 exports there via GitHub web.")
        return
    for csv in csvs:
        symbol, tf = _infer_from_filename(csv)
        print(f"[inbox] {csv.name} → symbol={symbol} timeframe={tf}")
        ingest_csv(str(csv), symbol, tf)


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
    p.add_argument("--inbox", action="store_true",
                   help="ingest every CSV in inbox/ (symbol+tf inferred from filename)")
    args = p.parse_args(argv[1:])

    (ROOT / "data").mkdir(parents=True, exist_ok=True)

    if args.inbox:
        ingest_inbox()
    elif args.from_csv:
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
