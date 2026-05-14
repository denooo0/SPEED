"""Historical CFTC Commitments-of-Traders archive for COMEX gold.

The CFTC publishes annual history files for the Disaggregated Futures-Only
report. Each year's archive is a ZIP containing a single TXT/CSV with one
row per market per weekly Tuesday snapshot. We download each year in the
caller-supplied range, filter to gold, normalise columns, and concatenate.

URL pattern (verified against the CFTC public archive):
    https://www.cftc.gov/files/dea/history/fut_disagg_txt_<year>.zip

The ZIP contains `f_year.txt` — a comma-separated header row + data rows
quoted with double quotes. Field order is documented in CFTC's
"Disaggregated Explanatory Notes". For ATLAS we collapse:

    commercial    = Producer/Merchant + Swap Dealer  (long, short)
    managed_money = Managed Money                    (long, short)
    small_spec    = Non-Reportable                   (long, short)

Network access is mockable: pass a `fetcher` callable returning the raw
ZIP bytes for a year, and a `parser` is exposed for unit testing.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


DEFAULT_URL_TEMPLATE = (
    "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
)

# CFTC disaggregated futures-only column names that we care about. These
# match the CSV header that ships in the archive (whitespace stripped).
COL_DATE = "Report_Date_as_YYYY-MM-DD"
COL_MARKET = "Market_and_Exchange_Names"
COL_PM_LONG = "Prod_Merc_Positions_Long_All"
COL_PM_SHORT = "Prod_Merc_Positions_Short_All"
COL_SD_LONG = "Swap_Positions_Long_All"
COL_SD_SHORT = "Swap__Positions_Short_All"  # CFTC double-underscore typo, kept for compat
COL_SD_SHORT_ALT = "Swap_Positions_Short_All"
COL_MM_LONG = "M_Money_Positions_Long_All"
COL_MM_SHORT = "M_Money_Positions_Short_All"
COL_NR_LONG = "NonRept_Positions_Long_All"
COL_NR_SHORT = "NonRept_Positions_Short_All"

GOLD_MARKET_TOKENS = ("GOLD", "COMMODITY EXCHANGE")


@dataclass
class COTArchiveConfig:
    url_template: str = DEFAULT_URL_TEMPLATE
    timeout_seconds: int = 60


FetcherFn = Callable[[int], bytes]


class COTArchive:
    """Multi-year CFTC COT history loader for COMEX gold."""

    def __init__(
        self,
        config: Optional[COTArchiveConfig] = None,
        fetcher: Optional[FetcherFn] = None,
    ) -> None:
        self.config = config or COTArchiveConfig()
        self._fetcher: FetcherFn = fetcher or self._default_fetcher

    # -- public ----------------------------------------------------------
    def fetch_history(self, start: datetime, end: datetime) -> pd.DataFrame:
        """Return weekly gold COT rows in [start, end] (inclusive).

        Columns: date, commercial_long, commercial_short, commercial_net,
        managed_money_long, managed_money_short, managed_money_net,
        small_spec_long, small_spec_short, small_spec_net.
        """
        start_utc = _ensure_utc(start)
        end_utc = _ensure_utc(end)
        if end_utc < start_utc:
            raise ValueError(f"end ({end}) must be >= start ({start})")

        frames: List[pd.DataFrame] = []
        for year in range(start_utc.year, end_utc.year + 1):
            try:
                payload = self._fetcher(year)
            except Exception as e:  # noqa: BLE001 — degrade gracefully
                logger.warning("COT archive %s fetch failed: %s", year, e)
                continue
            try:
                df = parse_cot_archive_zip(payload)
            except Exception as e:  # noqa: BLE001
                logger.warning("COT archive %s parse failed: %s", year, e)
                continue
            if not df.empty:
                frames.append(df)

        if not frames:
            return _empty_frame()

        combined = pd.concat(frames, ignore_index=True)
        combined["date"] = pd.to_datetime(combined["date"], utc=True, errors="coerce")
        combined = combined.dropna(subset=["date"])
        mask = (combined["date"] >= pd.Timestamp(start_utc)) & (
            combined["date"] <= pd.Timestamp(end_utc)
        )
        combined = combined.loc[mask].copy()
        combined = combined.drop_duplicates(subset=["date"], keep="last")
        combined = combined.sort_values("date").reset_index(drop=True)
        return combined

    # -- internals -------------------------------------------------------
    def _default_fetcher(self, year: int) -> bytes:
        import requests

        url = self.config.url_template.format(year=year)
        resp = requests.get(url, timeout=self.config.timeout_seconds)
        resp.raise_for_status()
        return resp.content


# -- parsing -------------------------------------------------------------


def parse_cot_archive_zip(zip_bytes: bytes) -> pd.DataFrame:
    """Parse a CFTC annual disaggregated futures-only ZIP archive into a
    DataFrame containing only gold rows in our normalised schema.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Choose the first .txt member (the year archive contains one).
        members = [n for n in zf.namelist() if n.lower().endswith((".txt", ".csv"))]
        if not members:
            raise ValueError("No TXT/CSV inside COT archive ZIP")
        with zf.open(members[0]) as fh:
            raw = fh.read()
    return parse_cot_archive_csv(raw)


def parse_cot_archive_csv(raw: bytes) -> pd.DataFrame:
    """Parse the CFTC disaggregated futures-only CSV bytes into gold rows."""
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [c.strip() for c in (reader.fieldnames or [])]
    if not fieldnames:
        return _empty_frame()

    # Build a quick lookup that tolerates surrounding whitespace.
    def pick(*candidates: str) -> Optional[str]:
        for cand in candidates:
            if cand in fieldnames:
                return cand
        return None

    date_col = pick(COL_DATE, "Report_Date_as_YYYY_MM_DD")
    market_col = pick(COL_MARKET)
    pm_l = pick(COL_PM_LONG)
    pm_s = pick(COL_PM_SHORT)
    sd_l = pick(COL_SD_LONG)
    sd_s = pick(COL_SD_SHORT, COL_SD_SHORT_ALT)
    mm_l = pick(COL_MM_LONG)
    mm_s = pick(COL_MM_SHORT)
    nr_l = pick(COL_NR_LONG)
    nr_s = pick(COL_NR_SHORT)

    required = [date_col, market_col, pm_l, pm_s, sd_l, sd_s, mm_l, mm_s, nr_l, nr_s]
    if any(c is None for c in required):
        missing = [
            n for n, c in zip(
                ["date", "market", "pm_long", "pm_short", "sd_long", "sd_short",
                 "mm_long", "mm_short", "nr_long", "nr_short"],
                required,
            ) if c is None
        ]
        raise ValueError(f"Missing CFTC columns: {missing}")

    rows: List[dict] = []
    for raw_row in reader:
        # Header may carry whitespace; rebuild a stripped row.
        row = {k.strip(): (v or "").strip() for k, v in raw_row.items() if k is not None}
        market = row.get(market_col, "")
        if not _is_gold_market(market):
            continue
        try:
            pm_long = _to_float(row[pm_l])
            pm_short = _to_float(row[pm_s])
            sd_long = _to_float(row[sd_l])
            sd_short = _to_float(row[sd_s])
            mm_long = _to_float(row[mm_l])
            mm_short = _to_float(row[mm_s])
            nr_long = _to_float(row[nr_l])
            nr_short = _to_float(row[nr_s])
        except (KeyError, ValueError):
            continue
        commercial_long = pm_long + sd_long
        commercial_short = pm_short + sd_short
        rows.append(
            {
                "date": row[date_col],
                "commercial_long": commercial_long,
                "commercial_short": commercial_short,
                "commercial_net": commercial_long - commercial_short,
                "managed_money_long": mm_long,
                "managed_money_short": mm_short,
                "managed_money_net": mm_long - mm_short,
                "small_spec_long": nr_long,
                "small_spec_short": nr_short,
                "small_spec_net": nr_long - nr_short,
            }
        )

    if not rows:
        return _empty_frame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return df


# -- helpers -------------------------------------------------------------


def _is_gold_market(market: str) -> bool:
    upper = market.upper()
    return all(tok in upper for tok in GOLD_MARKET_TOKENS)


def _to_float(value: str) -> float:
    if value is None:
        return 0.0
    cleaned = value.replace(",", "").replace('"', "").strip()
    if not cleaned or cleaned == ".":
        return 0.0
    return float(cleaned)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _empty_frame() -> pd.DataFrame:
    cols = [
        "date",
        "commercial_long",
        "commercial_short",
        "commercial_net",
        "managed_money_long",
        "managed_money_short",
        "managed_money_net",
        "small_spec_long",
        "small_spec_short",
        "small_spec_net",
    ]
    return pd.DataFrame(columns=cols)


# Re-export for callers that want to feed bytes directly.
__all__ = [
    "COTArchive",
    "COTArchiveConfig",
    "parse_cot_archive_zip",
    "parse_cot_archive_csv",
]
