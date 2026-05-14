"""FRED macro indicator history with yfinance fallback.

Phase 0 needs daily macro context (DXY, US10Y, VIX, oil) for the entire
backtest window. The FRED API (free key) is the canonical source. If
`FRED_API_KEY` isn't set we degrade to yfinance proxies — close enough for
backtest correlation work, exact identity not required.

Series mapping:
    DTWEXBGS    Trade-weighted USD broad index (DXY-ish)    → yf "DX-Y.NYB"
    DGS10       10-year Treasury constant maturity           → yf "^TNX"
    VIXCLS      CBOE VIX                                     → yf "^VIX"
    DCOILWTICO  WTI crude spot                               → yf "CL=F"

Returns a DataFrame with columns ["date", "value"] in chronological order.
Dates are tz-naive (calendar days); FRED publishes daily closes.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# Map FRED series → yfinance ticker proxy used when no API key is set.
DEFAULT_YF_FALLBACK: Dict[str, str] = {
    "DTWEXBGS": "DX-Y.NYB",
    "DGS10": "^TNX",
    "VIXCLS": "^VIX",
    "DCOILWTICO": "CL=F",
}


@dataclass
class FREDMacroConfig:
    api_key_env: str = "FRED_API_KEY"
    yf_fallback: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_YF_FALLBACK))


FredClientFactory = Callable[[str], "object"]
YFinanceDownload = Callable[..., pd.DataFrame]


class FREDMacro:
    """Daily macro series fetcher with optional yfinance proxy fallback."""

    def __init__(
        self,
        config: Optional[FREDMacroConfig] = None,
        api_key: Optional[str] = None,
        fred_client_factory: Optional[FredClientFactory] = None,
        yfinance_download: Optional[YFinanceDownload] = None,
    ) -> None:
        self.config = config or FREDMacroConfig()
        # Explicit param > env var > None.
        self.api_key = api_key if api_key is not None else os.environ.get(
            self.config.api_key_env
        )
        self._fred_factory = fred_client_factory
        self._yf_download = yfinance_download

    # -- public ----------------------------------------------------------
    def fetch_series(
        self,
        series_id: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Return daily `value` for `series_id` between start and end (incl.)."""
        start_utc = _ensure_utc(start)
        end_utc = _ensure_utc(end)
        if end_utc < start_utc:
            raise ValueError(f"end ({end}) must be >= start ({start})")

        if self.api_key:
            try:
                return self._fetch_via_fred(series_id, start_utc, end_utc)
            except Exception as e:  # noqa: BLE001 — fall back gracefully
                logger.warning(
                    "FRED fetch failed for %s (%s); falling back to yfinance",
                    series_id, e,
                )
        return self._fetch_via_yfinance(series_id, start_utc, end_utc)

    # -- internals -------------------------------------------------------
    def _fetch_via_fred(
        self,
        series_id: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        factory = self._fred_factory or _default_fred_factory
        client = factory(self.api_key)
        # fredapi's `Fred.get_series` returns a pandas Series indexed by date.
        series = client.get_series(
            series_id,
            observation_start=start.date().isoformat(),
            observation_end=end.date().isoformat(),
        )
        df = pd.DataFrame({"date": pd.to_datetime(series.index), "value": series.values})
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"]).sort_values("date").reset_index(drop=True)
        return df

    def _fetch_via_yfinance(
        self,
        series_id: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        ticker = self.config.yf_fallback.get(series_id)
        if ticker is None:
            logger.warning(
                "No yfinance fallback configured for %s; returning empty frame",
                series_id,
            )
            return _empty_frame()

        downloader = self._yf_download or _default_yfinance_download
        try:
            raw = downloader(
                ticker,
                start=start.date().isoformat(),
                # yfinance treats `end` exclusively, so bump by one day.
                end=(end.date() + pd.Timedelta(days=1).to_pytimedelta()).isoformat(),
                progress=False,
                auto_adjust=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("yfinance fallback failed for %s: %s", ticker, e)
            return _empty_frame()

        if raw is None or raw.empty:
            return _empty_frame()

        df = raw.copy()
        # Some yfinance versions return MultiIndex columns; flatten.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        # Prefer Close (FRED-style daily close); some series have only Adj Close.
        col = "Close" if "Close" in df.columns else (
            "Adj Close" if "Adj Close" in df.columns else df.columns[0]
        )
        out = pd.DataFrame(
            {
                "date": pd.to_datetime(df.index).tz_localize(None),
                "value": pd.to_numeric(df[col], errors="coerce").values,
            }
        )
        out = out.dropna(subset=["value"]).sort_values("date").reset_index(drop=True)
        return out


# -- defaults --------------------------------------------------------------

def _default_fred_factory(api_key: str):
    from fredapi import Fred  # local import keeps this dep optional

    return Fred(api_key=api_key)


def _default_yfinance_download(*args, **kwargs) -> pd.DataFrame:
    import yfinance  # local import keeps this dep optional

    return yfinance.download(*args, **kwargs)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({"date": pd.to_datetime([]), "value": pd.Series([], dtype=float)})


__all__ = ["FREDMacro", "FREDMacroConfig", "DEFAULT_YF_FALLBACK"]
