"""Parquet-backed dataset store.

One parquet file per logical dataset name. The store keeps a single root
directory and resolves `<root>/<name>.parquet`. Reads support optional
inclusive `start` / `end` filtering on either a `timestamp` column (Bybit
candles, FRED series indexed by date) or the DataFrame index when tz-aware.

Deliberately minimal — partition pruning and schema evolution are out of
scope for Phase 0. Our backtest replay touches all historical data anyway.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

import pandas as pd

logger = logging.getLogger(__name__)

WriteMode = Literal["overwrite", "append"]


class ParquetStore:
    """Stores DataFrames as `<root>/<name>.parquet`."""

    def __init__(self, root: Path = Path("data/")) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths -----------------------------------------------------------
    def _path(self, name: str) -> Path:
        # Allow names with slashes for namespacing (e.g. "bybit/XAUUSD_5m").
        safe = name.replace("..", "_")
        path = self.root / f"{safe}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # -- public ----------------------------------------------------------
    def has(self, name: str) -> bool:
        return self._path(name).exists()

    def list(self) -> List[str]:
        out: List[str] = []
        for p in self.root.rglob("*.parquet"):
            rel = p.relative_to(self.root)
            out.append(str(rel.with_suffix("")))
        return sorted(out)

    def write(
        self,
        name: str,
        df: pd.DataFrame,
        mode: WriteMode = "overwrite",
    ) -> Path:
        if df is None:
            raise ValueError("Cannot write None")
        path = self._path(name)
        if mode == "append" and path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, df])
            combined = self._dedupe_and_sort(combined)
            combined.to_parquet(path, index=self._index_is_meaningful(combined))
            return path
        df_out = self._dedupe_and_sort(df.copy())
        df_out.to_parquet(path, index=self._index_is_meaningful(df_out))
        return path

    def read(
        self,
        name: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> pd.DataFrame:
        path = self._path(name)
        if not path.exists():
            raise FileNotFoundError(f"No dataset at {path}")
        df = pd.read_parquet(path)
        if start is None and end is None:
            return df
        return self._filter_range(df, start, end)

    # -- internals -------------------------------------------------------
    @staticmethod
    def _index_is_meaningful(df: pd.DataFrame) -> bool:
        """Preserve a tz-aware DatetimeIndex but skip the default RangeIndex."""
        idx = df.index
        if isinstance(idx, pd.DatetimeIndex):
            return True
        return idx.name is not None

    @staticmethod
    def _dedupe_and_sort(df: pd.DataFrame) -> pd.DataFrame:
        if isinstance(df.index, pd.DatetimeIndex):
            df = df[~df.index.duplicated(keep="last")].sort_index()
            return df
        for col in ("timestamp", "date"):
            if col in df.columns:
                df = df.drop_duplicates(subset=[col], keep="last")
                df = df.sort_values(col).reset_index(drop=True)
                return df
        return df

    @staticmethod
    def _filter_range(
        df: pd.DataFrame,
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> pd.DataFrame:
        if isinstance(df.index, pd.DatetimeIndex):
            idx = df.index
            mask = pd.Series(True, index=idx)
            if start is not None:
                mask &= idx >= _coerce(start, idx.tz)
            if end is not None:
                mask &= idx <= _coerce(end, idx.tz)
            return df.loc[mask]
        for col in ("timestamp", "date"):
            if col in df.columns:
                series = pd.to_datetime(df[col], utc=True, errors="coerce")
                mask = pd.Series(True, index=df.index)
                if start is not None:
                    mask &= series >= _coerce(start, "UTC")
                if end is not None:
                    mask &= series <= _coerce(end, "UTC")
                return df.loc[mask].reset_index(drop=True)
        return df


def _coerce(ts: datetime, tz) -> pd.Timestamp:
    """Coerce a datetime to a pandas Timestamp matching the target tz."""
    out = pd.Timestamp(ts)
    if tz is None:
        if out.tzinfo is not None:
            out = out.tz_convert(None)
        return out
    if out.tzinfo is None:
        return out.tz_localize(tz)
    return out.tz_convert(tz)
