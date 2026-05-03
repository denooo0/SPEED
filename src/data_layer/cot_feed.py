"""CFTC Commitments of Traders feed.

Fetches the weekly COT report (Disaggregated, Futures-Only) for COMEX gold
(GC) and parses positioning into commercial / managed-money / small-spec
buckets. The report drops Friday afternoon for the prior Tuesday's data.

Cached to disk to avoid re-fetching on every cycle.

Network access is optional: if you can't reach CFTC from your VPS, drop a
parsed COT JSON into `cot_cache/latest.json` manually with the same shape
and the feature will work.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# CFTC publishes the disaggregated futures-only short report as a TXT/CSV.
# This URL is stable across years; the file contains all commodities.
DEFAULT_URL = (
    "https://www.cftc.gov/dea/newcot/FinFutWk.txt"
)
GOLD_MARKET_NAMES = (
    "GOLD - COMMODITY EXCHANGE INC.",
    "GOLD - COMMODITY EXCHANGE, INC.",
    "GC - COMMODITY EXCHANGE INC.",
)
CACHE_TTL_SECONDS = 6 * 60 * 60  # report ships once a week; refresh every 6h


@dataclass
class COTSnapshot:
    """Per-bucket net positioning. All values in contracts."""
    report_date: str           # "YYYY-MM-DD" (Tuesday of the report week)
    commercial_long: float
    commercial_short: float
    commercial_net: float
    managed_money_long: float
    managed_money_short: float
    managed_money_net: float
    small_spec_long: float
    small_spec_short: float
    small_spec_net: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def commercial_bias(self) -> str:
        if self.commercial_net > 0:
            return "long"
        if self.commercial_net < 0:
            return "short"
        return "neutral"

    @property
    def managed_money_bias(self) -> str:
        if self.managed_money_net > 0:
            return "long"
        if self.managed_money_net < 0:
            return "short"
        return "neutral"


@dataclass
class COTConfig:
    cache_dir: str = "cot_cache/"
    cache_filename: str = "latest.json"
    url: str = DEFAULT_URL
    fetch_timeout_seconds: int = 30


class COTFeed:
    """Reads + caches the latest COT positioning for gold."""

    def __init__(self, config: Optional[COTConfig] = None) -> None:
        self.config = config or COTConfig()
        Path(self.config.cache_dir).mkdir(parents=True, exist_ok=True)

    @property
    def _cache_path(self) -> Path:
        return Path(self.config.cache_dir) / self.config.cache_filename

    # -- public ----------------------------------------------------------
    def latest(self, force_refresh: bool = False) -> Optional[COTSnapshot]:
        """Return the cached snapshot. Refreshes if stale or absent."""
        if not force_refresh and self._cache_is_fresh():
            return self._read_cache()
        try:
            snapshot = self._fetch_and_parse()
        except Exception as e:  # noqa: BLE001 — degrade gracefully
            logger.warning("COT fetch failed (%s); falling back to cache if any", e)
            return self._read_cache()
        if snapshot is not None:
            self._write_cache(snapshot)
        return snapshot

    # -- internals -------------------------------------------------------
    def _cache_is_fresh(self) -> bool:
        if not self._cache_path.exists():
            return False
        age = time.time() - self._cache_path.stat().st_mtime
        return age < CACHE_TTL_SECONDS

    def _read_cache(self) -> Optional[COTSnapshot]:
        if not self._cache_path.exists():
            return None
        try:
            data = json.loads(self._cache_path.read_text())
            return COTSnapshot(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("COT cache corrupted: %s", e)
            return None

    def _write_cache(self, snapshot: COTSnapshot) -> None:
        self._cache_path.write_text(json.dumps(snapshot.to_dict(), indent=2))

    def _fetch_and_parse(self) -> Optional[COTSnapshot]:
        # Lazy import — keep `requests` out of the import path of unrelated tests
        import requests
        resp = requests.get(self.config.url, timeout=self.config.fetch_timeout_seconds)
        resp.raise_for_status()
        return parse_cot_text(resp.text)


def parse_cot_text(text: str) -> Optional[COTSnapshot]:
    """Parse the CFTC weekly TXT into a COTSnapshot for gold.

    The CFTC TXT format is a tabular plaintext dump with one line per
    market. We locate the gold rows by name, then pull the disaggregated
    long/short fields by fixed offsets (column header line precedes them).

    The wire format is brittle — CFTC does occasionally tweak it. We rely
    on the line containing the gold market name and split by whitespace.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        upper = line.upper()
        if any(name in upper for name in GOLD_MARKET_NAMES):
            # The COT short-format includes a date line nearby; search up to 5
            # lines for it.
            report_date = _find_report_date(lines, max(0, i - 5), i + 5)
            # Numeric block follows on subsequent lines; pull the first row of
            # 12+ numbers we encounter.
            numbers = _first_number_row(lines, i + 1, i + 20, min_count=12)
            if numbers is None:
                continue
            return _build_snapshot_from_numbers(numbers, report_date)
    return None


def _find_report_date(lines: list[str], start: int, end: int) -> str:
    """Look for `as of <Mon> dd, YYYY` style label in nearby lines."""
    import re
    pattern = re.compile(
        r"as\s+of\s+(?P<m>\w{3,9})\s+(?P<d>\d{1,2})[,\s]+(?P<y>\d{4})",
        re.IGNORECASE,
    )
    for ln in lines[start: end]:
        match = pattern.search(ln)
        if match:
            try:
                from datetime import datetime
                dt = datetime.strptime(
                    f"{match.group('m')} {match.group('d')} {match.group('y')}",
                    "%B %d %Y",
                )
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(
                        f"{match.group('m')} {match.group('d')} {match.group('y')}",
                        "%b %d %Y",
                    )
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue
    return ""


def _first_number_row(
    lines: list[str], start: int, end: int, min_count: int
) -> Optional[list[float]]:
    import re
    for ln in lines[start: end]:
        # COT files use commas as thousands separators; strip them
        cleaned = ln.replace(",", "")
        tokens = re.findall(r"-?\d+(?:\.\d+)?", cleaned)
        if len(tokens) >= min_count:
            try:
                return [float(t) for t in tokens]
            except ValueError:
                continue
    return None


def _build_snapshot_from_numbers(nums: list[float], report_date: str) -> COTSnapshot:
    """Map the first six longs/shorts into commercial / managed-money / small-spec.

    Disaggregated futures-only short report column order (after open interest):
      Producer/Merchant Long, Short
      Swap Dealer Long, Short, Spreading
      Managed Money Long, Short, Spreading
      Other Reportables Long, Short, Spreading
      Total Reportable Long, Short
      Non-Reportable (small spec) Long, Short

    For ATLAS purposes we collapse:
      commercial = Producer/Merchant + Swap Dealer
      managed_money = Managed Money
      small_spec = Non-Reportable
    """
    # The base disaggregated row is 15 numbers (PM2 + SD3 + MM3 + Other3 +
    # Total2 + NonRep2). Some snapshots prepend Open Interest as a 16th
    # column; detect by length.
    cursor = 1 if len(nums) >= 16 else 0
    def take(n: int) -> list[float]:
        nonlocal cursor
        out = nums[cursor: cursor + n]
        cursor += n
        return out

    pm_long, pm_short = take(2)
    sd_long, sd_short, _ = take(3)
    mm_long, mm_short, _ = take(3)
    # Need 7 more to reach + read small-spec: Other(3) + Total(2) skipped, then take 2.
    small_long = small_short = 0.0
    if len(nums) - cursor >= 7:
        cursor += 5  # Other L/S/Sp + Total L/S
        small_long, small_short = take(2)

    commercial_long = pm_long + sd_long
    commercial_short = pm_short + sd_short
    return COTSnapshot(
        report_date=report_date,
        commercial_long=commercial_long,
        commercial_short=commercial_short,
        commercial_net=commercial_long - commercial_short,
        managed_money_long=mm_long,
        managed_money_short=mm_short,
        managed_money_net=mm_long - mm_short,
        small_spec_long=small_long,
        small_spec_short=small_short,
        small_spec_net=small_long - small_short,
    )
