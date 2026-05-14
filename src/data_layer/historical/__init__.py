"""Historical data acquisition pipeline for ATLAS Phase 0 backtest.

Modules:
    bybit_historical: paginated OHLCV download via the existing BybitConnector.
    cot_archive: multi-year CFTC Commitments-of-Traders history for COMEX gold.
    fred_macro: FRED daily macro series with yfinance fallback when no API key.
    storage: thin parquet store keyed by logical dataset name.
"""
from __future__ import annotations

from .bybit_historical import BybitHistoricalFetcher
from .cot_archive import COTArchive
from .fred_macro import FREDMacro
from .storage import ParquetStore

__all__ = [
    "BybitHistoricalFetcher",
    "COTArchive",
    "FREDMacro",
    "ParquetStore",
]
