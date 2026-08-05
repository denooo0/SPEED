"""Load OHLCV bars from disk and derive the market context MIRROR needs.

START_HERE assumes you can already produce two things it never shows you how to
build: a list of `Bar` covering the same span as your Telegram archive, and the
anchor set (`swing_high=...`, `vwap=...`, `vpoc=...`) that Step 4 feeds to the
provenance miner. This module is that missing half.

Two rules govern everything here:

1. **Point-in-time or nothing.** Every context value handed to the miner is
   computed from bars that had already CLOSED at the signal's timestamp. A
   swing high that only became a swing high an hour later is not something the
   channel could have anchored to, and letting one leak in manufactures a rule
   that never existed.
2. **Missing beats guessed.** Any anchor that cannot be computed honestly from
   the available history is left as None. `build_context` already treats None
   as "this anchor does not exist", so the miner simply never attributes a
   level to it — which is the correct outcome, and far better than a fabricated
   number that becomes a confident, wrong rule.
"""
from __future__ import annotations

import csv
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .core.types import Bar, Quality
from .mirror.provenance import MarketContext, build_context

NS = 1_000_000_000

# Column aliases seen across brokers, Dukascopy, Polygon, Twelve Data, MT4/MT5
# exports and TradingView dumps. Matching is case/space/underscore-insensitive.
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "t": ("time", "timestamp", "datetime", "date", "t", "open_time", "opentime",
          "gmt time", "local time", "date_time", "ts"),
    "o": ("open", "o", "openprice", "open_price", "bidopen"),
    "h": ("high", "h", "highprice", "high_price", "bidhigh"),
    "l": ("low", "l", "lowprice", "low_price", "bidlow"),
    "c": ("close", "c", "closeprice", "close_price", "bidclose", "last"),
    "v": ("volume", "vol", "v", "tickvol", "tick_volume", "tickvolume",
          "real_volume", "volumefrom", "quantity"),
    "n": ("ticks", "n", "trades", "count", "num_trades", "transactions"),
    "date_only": ("date",),
    "time_only": ("time",),
}

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y%m%d %H:%M:%S",
    "%Y-%m-%d", "%Y.%m.%d", "%d.%m.%Y", "%m/%d/%Y",
)


class BarLoadError(Exception):
    """Raised when a file cannot be read as bars. Always names the fix."""


def _norm(s: str) -> str:
    return s.strip().lower().replace("_", "").replace(" ", "").replace("-", "")


def _resolve_columns(header: Sequence[str]) -> Dict[str, int]:
    """Map canonical field -> column index, by alias, case-insensitively."""
    idx: Dict[str, int] = {}
    normed = [_norm(h) for h in header]
    for field, aliases in _ALIASES.items():
        if field in ("date_only", "time_only"):
            continue
        for alias in aliases:
            a = _norm(alias)
            if a in normed:
                idx[field] = normed.index(a)
                break
    return idx


def _parse_time(raw: str, tz_offset_s: int) -> int:
    """Parse a timestamp cell into epoch nanoseconds (UTC)."""
    raw = raw.strip().strip('"')
    if not raw:
        raise BarLoadError("empty timestamp cell")

    # Bare epoch: seconds, milliseconds, microseconds or nanoseconds.
    if raw.replace(".", "", 1).replace("-", "", 1).isdigit() and "-" not in raw[1:]:
        try:
            val = float(raw)
        except ValueError:
            val = None
        if val is not None and val > 10_000_000:  # too big to be a bare date
            if val >= 1e17:
                return int(val)
            if val >= 1e14:
                return int(val * 1_000)
            if val >= 1e11:
                return int(val * 1_000_000)
            return int(val * NS)

    txt = raw.replace("T", " ").rstrip("Z")
    # Trim fractional seconds; we key on whole seconds.
    if "." in txt and ":" in txt:
        head, _, tail = txt.rpartition(".")
        if tail.isdigit():
            txt = head
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(txt, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=timezone.utc).timestamp()) * NS - tz_offset_s * NS
    raise BarLoadError(
        f"unrecognised timestamp {raw!r}. Supported formats: "
        f"{', '.join(_TIME_FORMATS[:6])}, or a bare epoch."
    )


def _f(cell: str) -> float:
    return float(str(cell).strip().strip('"').replace(",", ""))


def load_bars(path: str | Path, *, interval_s: Optional[int] = None,
              tz_offset_hours: float = 0.0,
              symbol_filter: Optional[str] = None) -> List[Bar]:
    """Read a CSV or Parquet file of OHLCV bars into `Bar` objects.

    `tz_offset_hours` is the UTC offset the file's timestamps are already in
    (e.g. a broker exporting UTC+2 server time -> pass 2). It is subtracted to
    bring everything to UTC. This matters more than it looks: forced-flow
    windows are anchored to London and New York local time, so a two-hour
    timestamp error moves every signal into the wrong session and quietly
    invalidates Step 5.

    `interval_s` is inferred from the median gap between bars when omitted.
    Every returned bar has `closed=True` — these are historical bars that are,
    by definition, finished.
    """
    p = Path(path)
    if not p.exists():
        raise BarLoadError(f"no such file: {p}")

    if p.suffix.lower() in (".parquet", ".pq"):
        rows = _read_parquet(p)
    else:
        rows = _read_csv(p)

    if not rows:
        raise BarLoadError(f"{p} contained no data rows")

    tz_off = int(round(tz_offset_hours * 3600))
    recs: List[Tuple[int, float, float, float, float, float, int]] = []
    for r in rows:
        if symbol_filter:
            sym = r.get("symbol") or r.get("ticker") or ""
            if sym and _norm(str(sym)) != _norm(symbol_filter):
                continue
        t_ns = r["t"] if isinstance(r["t"], int) else _parse_time(str(r["t"]), tz_off)
        try:
            o, h, l, c = _f(r["o"]), _f(r["h"]), _f(r["l"]), _f(r["c"])
        except (ValueError, TypeError) as e:
            raise BarLoadError(f"non-numeric OHLC in {p}: {e}") from e
        v = _f(r["v"]) if r.get("v") not in (None, "") else 0.0
        n = int(_f(r["n"])) if r.get("n") not in (None, "") else 0
        if not (h >= max(o, c) and l <= min(o, c)):
            continue  # incoherent bar; drop rather than reason about it
        recs.append((t_ns, o, h, l, c, v, n))

    if not recs:
        raise BarLoadError(
            f"{p}: every row was filtered out. "
            f"{'Check --symbol matches the file. ' if symbol_filter else ''}"
            "Rows are dropped when high < max(open,close) or low > min(open,close)."
        )

    recs.sort(key=lambda x: x[0])
    # Drop exact duplicate timestamps, keeping the last occurrence (a revision
    # of a bar beats the original print).
    deduped: List[Tuple[int, float, float, float, float, float, int]] = []
    for rec in recs:
        if deduped and deduped[-1][0] == rec[0]:
            deduped[-1] = rec
        else:
            deduped.append(rec)

    if interval_s is None:
        interval_s = _infer_interval(d[0] for d in deduped)

    out: List[Bar] = []
    for t_ns, o, h, l, c, v, n in deduped:
        out.append(Bar(
            t_open_ns=t_ns, t_close_ns=t_ns + interval_s * NS,
            interval_s=interval_s, o=o, h=h, l=l, c=c,
            volume=v, ticks=n or 1, closed=True, quality=Quality.CLEAN,
        ))
    return out


def _read_csv(p: Path) -> List[Dict[str, object]]:
    with open(p, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        try:
            header = next(reader)
        except StopIteration:
            return []
        idx = _resolve_columns(header)
        missing = [k for k in ("t", "o", "h", "l", "c") if k not in idx]
        if missing:
            raise BarLoadError(
                f"{p}: could not find column(s) for {missing}. "
                f"Header was: {header}. Rename the columns to "
                f"time,open,high,low,close,volume."
            )
        sym_i = next((i for i, h in enumerate(header)
                      if _norm(h) in ("symbol", "ticker")), None)
        rows: List[Dict[str, object]] = []
        for row in reader:
            if not row or len(row) <= max(idx.values()):
                continue
            d: Dict[str, object] = {k: row[i] for k, i in idx.items()}
            if sym_i is not None and sym_i < len(row):
                d["symbol"] = row[sym_i]
            rows.append(d)
        return rows


def _read_parquet(p: Path) -> List[Dict[str, object]]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as e:
        raise BarLoadError(
            f"{p} is Parquet but pyarrow is not installed. "
            "Run: pip install pyarrow  (or export the data to CSV)"
        ) from e
    table = pq.read_table(p)
    cols = list(table.column_names)
    idx = _resolve_columns(cols)
    missing = [k for k in ("t", "o", "h", "l", "c") if k not in idx]
    if missing:
        raise BarLoadError(
            f"{p}: could not find column(s) for {missing}. Columns were: {cols}"
        )
    data = table.to_pydict()
    name = {k: cols[i] for k, i in idx.items()}
    n_rows = table.num_rows
    sym_col = next((c for c in cols if _norm(c) in ("symbol", "ticker")), None)
    rows: List[Dict[str, object]] = []
    for i in range(n_rows):
        d: Dict[str, object] = {}
        for k, col in name.items():
            val = data[col][i]
            if k == "t" and isinstance(val, datetime):
                if val.tzinfo is None:
                    val = val.replace(tzinfo=timezone.utc)
                d[k] = int(val.timestamp()) * NS
            else:
                d[k] = val
        if sym_col:
            d["symbol"] = data[sym_col][i]
        rows.append(d)
    return rows


def _infer_interval(times: Iterable[int]) -> int:
    ts = list(times)
    gaps = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    if not gaps:
        return 60
    # Median gap resists weekend/holiday holes far better than the mean.
    return max(1, gaps[len(gaps) // 2] // NS)


def bars_after(bars: Sequence[Bar], t_ns: int, limit: int = 5000) -> List[Bar]:
    """Bars that OPENED strictly at or after `t_ns` — the forward tape.

    This is what `replay_signal` walks. The strict cutoff matters: including the
    bar a signal was posted inside lets the replay fill at a price that had
    already printed before the post, which flatters every result.
    """
    opens = [b.t_open_ns for b in bars]
    i = bisect_left(opens, t_ns)
    return list(bars[i:i + limit])


def bars_before(bars: Sequence[Bar], t_ns: int) -> List[Bar]:
    """Bars fully CLOSED at `t_ns` — the only history a signal could have used."""
    closes = [b.t_close_ns for b in bars]
    return list(bars[:bisect_right(closes, t_ns)])


@dataclass
class _Swing:
    price: float
    t_ns: int


class ContextBuilder:
    """Derives point-in-time anchors for Step 4's provenance mining.

    Construct once over your full bar history, then call `at(t_ns)` per signal.
    Everything it returns is computed only from bars closed at `t_ns`.
    """

    def __init__(self, bars: Sequence[Bar], *, atr_period: int = 14,
                 swing_lookback: int = 3, session_tz_offset_h: int = 0):
        self.bars = list(bars)
        self.atr_period = atr_period
        self.swing_lookback = swing_lookback
        self.session_tz_offset_h = session_tz_offset_h
        self._closes = [b.t_close_ns for b in self.bars]

    def _index(self, t_ns: int) -> int:
        """Number of bars fully closed at t_ns."""
        return bisect_right(self._closes, t_ns)

    def _atr(self, i: int) -> Optional[float]:
        if i < self.atr_period + 1:
            return None
        trs = []
        for j in range(i - self.atr_period, i):
            prev_c = self.bars[j - 1].c
            b = self.bars[j]
            trs.append(max(b.h - b.l, abs(b.h - prev_c), abs(b.l - prev_c)))
        return sum(trs) / len(trs) if trs else None

    def _swings(self, i: int) -> Tuple[Optional[float], Optional[float],
                                       Optional[float], Optional[float]]:
        """Most recent and prior confirmed fractal swing high/low.

        A swing is only 'confirmed' once `swing_lookback` bars have closed on
        BOTH sides of it — which is the whole point. An unconfirmed swing is
        hindsight.
        """
        k = self.swing_lookback
        highs: List[_Swing] = []
        lows: List[_Swing] = []
        # Only scan a bounded window back; anchors older than this are stale.
        start = max(k, i - 500)
        for j in range(start, i - k):
            b = self.bars[j]
            window = self.bars[j - k:j + k + 1]
            if len(window) < 2 * k + 1:
                continue
            if all(b.h >= w.h for w in window):
                highs.append(_Swing(b.h, b.t_open_ns))
            if all(b.l <= w.l for w in window):
                lows.append(_Swing(b.l, b.t_open_ns))
        sh = highs[-1].price if highs else None
        psh = highs[-2].price if len(highs) > 1 else None
        sl = lows[-1].price if lows else None
        psl = lows[-2].price if len(lows) > 1 else None
        return sh, sl, psh, psl

    def _session_bounds(self, i: int) -> Tuple[int, int]:
        """[start, end) bar indices of the UTC day containing bar i-1."""
        if i <= 0:
            return 0, 0
        day_ns = 86_400 * NS
        off = self.session_tz_offset_h * 3600 * NS
        day = (self.bars[i - 1].t_open_ns + off) // day_ns
        start = i - 1
        while start > 0 and (self.bars[start - 1].t_open_ns + off) // day_ns == day:
            start -= 1
        return start, i

    def _profile(self, lo_i: int, hi_i: int) -> Tuple[Optional[float],
                                                      Optional[float],
                                                      Optional[float]]:
        """Volume point-of-control and 70% value area over a bar slice.

        Volume is spread uniformly across each bar's range — a real tick
        profile is better, but this is honest about what 1m bars can support
        and is stable enough to attribute levels against.
        """
        seg = self.bars[lo_i:hi_i]
        if len(seg) < 5:
            return None, None, None
        lo = min(b.l for b in seg)
        hi = max(b.h for b in seg)
        if hi <= lo:
            return None, None, None
        n_bins = 100
        width = (hi - lo) / n_bins
        bins = [0.0] * n_bins
        for b in seg:
            vol = b.volume or float(b.ticks) or 1.0
            b_lo = int((b.l - lo) / width)
            b_hi = int((b.h - lo) / width)
            b_lo = max(0, min(n_bins - 1, b_lo))
            b_hi = max(0, min(n_bins - 1, b_hi))
            span = b_hi - b_lo + 1
            share = vol / span
            for k in range(b_lo, b_hi + 1):
                bins[k] += share
        total = sum(bins)
        if total <= 0:
            return None, None, None
        poc_i = max(range(n_bins), key=lambda k: bins[k])
        vpoc = lo + (poc_i + 0.5) * width
        # Expand outward from the POC until 70% of volume is enclosed.
        acc = bins[poc_i]
        left = right = poc_i
        while acc < 0.70 * total and (left > 0 or right < n_bins - 1):
            take_l = bins[left - 1] if left > 0 else -1.0
            take_r = bins[right + 1] if right < n_bins - 1 else -1.0
            if take_r >= take_l:
                right += 1
                acc += bins[right]
            else:
                left -= 1
                acc += bins[left]
        return vpoc, lo + (right + 1) * width, lo + left * width

    def at(self, t_ns: int) -> Optional[MarketContext]:
        """Full anchor set as of `t_ns`, or None if history is too thin."""
        i = self._index(t_ns)
        if i < self.atr_period + 2:
            return None
        atr = self._atr(i)
        if not atr or atr <= 0:
            return None
        mid = self.bars[i - 1].c

        sh, sl, psh, psl = self._swings(i)
        s_lo, s_hi = self._session_bounds(i)
        seg = self.bars[s_lo:s_hi]
        session_high = max((b.h for b in seg), default=None)
        session_low = min((b.l for b in seg), default=None)

        # Previous session
        prev_high = prev_low = prev_close = None
        if s_lo > 0:
            p_lo, _ = self._session_bounds(s_lo)
            pseg = self.bars[p_lo:s_lo]
            if pseg:
                prev_high = max(b.h for b in pseg)
                prev_low = min(b.l for b in pseg)
                prev_close = pseg[-1].c

        # Session VWAP and its standard-deviation bands
        vwap = u1 = l1 = u2 = l2 = None
        if seg:
            num = sum(b.typical * (b.volume or b.ticks or 1) for b in seg)
            den = sum((b.volume or b.ticks or 1) for b in seg)
            if den > 0:
                vwap = num / den
                var = sum(((b.typical - vwap) ** 2) * (b.volume or b.ticks or 1)
                          for b in seg) / den
                sd = math.sqrt(max(0.0, var))
                u1, l1 = vwap + sd, vwap - sd
                u2, l2 = vwap + 2 * sd, vwap - 2 * sd

        vpoc, vah, val = self._profile(s_lo, s_hi)

        return build_context(
            t_ns, mid, atr,
            swing_high=sh, swing_low=sl,
            prev_swing_high=psh, prev_swing_low=psl,
            vwap=vwap, vwap_u1=u1, vwap_l1=l1, vwap_u2=u2, vwap_l2=l2,
            session_high=session_high, session_low=session_low,
            prev_day_high=prev_high, prev_day_low=prev_low,
            prev_day_close=prev_close,
            vpoc=vpoc, value_area_high=vah, value_area_low=val,
        )


def swept_side(prev: Bar, cur: Bar) -> Optional[str]:
    """Did `cur` take out `prev`'s extreme and close back inside it?

    Step 5's `ff.update(bar, swept=sweep_flag(bar))` needs a sweep flag and the
    guide leaves it undefined. This is the conservative reading: a level was
    swept only if price traded through it AND failed to hold, which is what
    distinguishes a liquidity grab from a genuine break.
    """
    if cur.h > prev.h and cur.c < prev.h:
        return "high"
    if cur.l < prev.l and cur.c > prev.l:
        return "low"
    return None
