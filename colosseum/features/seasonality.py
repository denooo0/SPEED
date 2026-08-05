"""Intraday seasonality — the most underexploited real edge inside the constraint.

Gold has powerful, persistent, mechanically-caused intraday structure, and almost
none of it comes from prediction. It comes from FORCED FLOW — participants who
must trade at particular times for reasons unrelated to their price view:

  * the London AM/PM gold fixes (benchmark-linked flow)
  * the Asia → London handover, where liquidity steps up abruptly
  * the London → NY overlap, the highest-volume window of the day
  * NY futures pit open, and the COMEX settlement window
  * the daily roll, where the Asia range often defines the day's initial balance

That is edge source #3 from the taxonomy — structural, not informational — and
it is fully visible in OHLCV. You do not need to know WHY someone must trade at
15:00 London to observe that they reliably do.

WHAT THIS MODULE REFUSES TO DO. It will not hand you "hour 13 is bullish."
Time-of-day analysis is a multiple-testing minefield: 24 hours × 2 directions ×
several statistics is dozens of implicit tests, and something always looks
significant. So every cell carries a t-statistic, an FDR-corrected significance
flag, and a stability score across non-overlapping halves. A pattern that is
strong in the first half and absent in the second is reported as UNSTABLE, not
as an edge.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..core.clock import NS, liquidity_session, ns_to_dt
from ..core.types import Bar


@dataclass
class Cell:
    """One time bucket's accumulated statistics."""
    key: str
    n: int = 0
    sum_ret: float = 0.0
    sum_sq: float = 0.0
    sum_abs: float = 0.0
    sum_range: float = 0.0
    sum_vol: float = 0.0
    up: int = 0
    sweeps_high: int = 0
    sweeps_low: int = 0
    first_half_ret: float = 0.0
    first_half_n: int = 0
    second_half_ret: float = 0.0
    second_half_n: int = 0

    @property
    def mean_ret(self) -> float:
        return self.sum_ret / self.n if self.n else 0.0

    @property
    def std_ret(self) -> float:
        if self.n < 2:
            return 0.0
        v = (self.sum_sq - self.n * self.mean_ret ** 2) / (self.n - 1)
        return math.sqrt(max(v, 0.0))

    @property
    def mean_abs(self) -> float:
        """Realized volatility proxy. The MOST reliable seasonal signal — far
        more stable than directional drift, and directly useful: it tells you
        where to place stops and how far targets can realistically reach."""
        return self.sum_abs / self.n if self.n else 0.0

    @property
    def up_rate(self) -> float:
        return self.up / self.n if self.n else 0.0

    @property
    def t_stat(self) -> float:
        s = self.std_ret
        if self.n < 3 or s <= 0:
            return 0.0
        return self.mean_ret / (s / math.sqrt(self.n))

    @property
    def stability(self) -> float:
        """Agreement between the first and second halves of the sample.

        1.0 = same sign and similar magnitude. <=0 = the pattern reversed, which
        is the signature of a fitted artifact rather than a structural effect.
        """
        if self.first_half_n < 5 or self.second_half_n < 5:
            return 0.0
        a = self.first_half_ret / self.first_half_n
        b = self.second_half_ret / self.second_half_n
        if a == 0 and b == 0:
            return 0.0
        if a * b <= 0:
            return 0.0
        return min(abs(a), abs(b)) / max(abs(a), abs(b))

    def to_dict(self) -> Dict[str, object]:
        return {"key": self.key, "n": self.n,
                "mean_ret_bp": round(self.mean_ret * 10000, 2),
                "mean_abs_bp": round(self.mean_abs * 10000, 2),
                "up_rate": round(self.up_rate, 4),
                "t_stat": round(self.t_stat, 3),
                "stability": round(self.stability, 3),
                "sweep_high_rate": round(self.sweeps_high / self.n, 4) if self.n else 0,
                "sweep_low_rate": round(self.sweeps_low / self.n, 4) if self.n else 0}


def _bh_significant(t_stats: Sequence[float], fdr: float = 0.10) -> List[bool]:
    """Benjamini-Hochberg over the whole grid of buckets.

    Without this, testing 24 hours guarantees a couple of 'significant' results
    from noise alone. With it, the bar rises with the number of buckets tested.
    """
    def _p(t: float) -> float:
        z = abs(t)
        return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
    ps = [_p(t) for t in t_stats]
    n = len(ps)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: ps[i])
    k_max = -1
    for rank, i in enumerate(order, start=1):
        if ps[i] <= fdr * rank / n:
            k_max = rank
    flags = [False] * n
    if k_max > 0:
        for rank, i in enumerate(order, start=1):
            if rank <= k_max:
                flags[i] = True
    return flags


class SeasonalityEngine:
    """Accumulates conditional statistics by hour, session, and weekday.

    Everything is computed on CLOSED bars, point-in-time. The engine is a
    read-only observer of history; nothing here can leak forward.
    """

    def __init__(self, split_at_ns: Optional[int] = None):
        self.by_hour: Dict[int, Cell] = {h: Cell(f"utc_hour_{h:02d}")
                                         for h in range(24)}
        self.by_session: Dict[str, Cell] = {}
        self.by_weekday_hour: Dict[Tuple[int, int], Cell] = {}
        self.split_at_ns = split_at_ns      # for the stability test
        self._total = 0
        self._t_min = 0
        self._t_max = 0

    def update(self, b: Bar, swept: Optional[str] = None) -> None:
        if b.o <= 0 or not b.closed:
            return
        dt = ns_to_dt(b.t_open_ns)
        ret = (b.c - b.o) / b.o
        hour = dt.hour
        wd = dt.weekday()
        sess = liquidity_session(b.t_open_ns)

        self._total += 1
        self._t_min = self._t_min or b.t_open_ns
        self._t_max = max(self._t_max, b.t_open_ns)
        split = self.split_at_ns or 0

        for cell in (self.by_hour[hour],
                     self.by_session.setdefault(sess, Cell(f"session_{sess}")),
                     self.by_weekday_hour.setdefault((wd, hour),
                                                     Cell(f"wd{wd}_h{hour:02d}"))):
            cell.n += 1
            cell.sum_ret += ret
            cell.sum_sq += ret * ret
            cell.sum_abs += abs(ret)
            cell.sum_range += (b.h - b.l) / b.o
            cell.sum_vol += b.volume
            cell.up += 1 if b.c >= b.o else 0
            if swept == "sweep_high":
                cell.sweeps_high += 1
            elif swept == "sweep_low":
                cell.sweeps_low += 1
            if split and b.t_open_ns < split:
                cell.first_half_ret += ret
                cell.first_half_n += 1
            elif split:
                cell.second_half_ret += ret
                cell.second_half_n += 1

    def auto_split(self) -> None:
        """Set the stability split at the sample midpoint if not given."""
        if not self.split_at_ns and self._t_min and self._t_max:
            self.split_at_ns = (self._t_min + self._t_max) // 2

    # ---- reads -----------------------------------------------------------

    def volatility_profile(self) -> Dict[int, float]:
        """Expected absolute move by hour, normalized to the daily mean.

        This is the highest-confidence output of the module and it is directly
        actionable: a stop sized for the NY overlap is far too tight for Asia,
        and a target that is reasonable at 14:00 is fantasy at 02:00. Feeding
        this into the bandit's ATR multipliers is close to free improvement.
        """
        vals = {h: c.mean_abs for h, c in self.by_hour.items() if c.n >= 20}
        if not vals:
            return {}
        avg = sum(vals.values()) / len(vals)
        return {h: round(v / avg, 4) for h, v in sorted(vals.items())} if avg else {}

    def significant_hours(self, fdr: float = 0.10,
                          min_n: int = 60,
                          min_stability: float = 0.35) -> List[Dict[str, object]]:
        """Directional hours that survive multiple-testing AND the stability test.

        Two gates, because they catch different lies: FDR catches "something in
        24 buckets always looks good," and stability catches "it worked in 2023
        and stopped." A pattern must pass both to be reported as real.
        """
        cells = [c for c in self.by_hour.values() if c.n >= min_n]
        if not cells:
            return []
        flags = _bh_significant([c.t_stat for c in cells], fdr)
        out = []
        for c, sig in zip(cells, flags):
            stable = c.stability >= min_stability
            if sig and stable:
                verdict = "REAL: survives FDR correction and is stable across halves"
            elif sig and not stable:
                verdict = (f"UNSTABLE: statistically significant but stability "
                           f"{c.stability:.2f} — present in one half of the "
                           f"sample and not the other. Do not trade this.")
            else:
                continue
            d = c.to_dict()
            d.update({"significant": sig, "stable": stable, "verdict": verdict})
            out.append(d)
        return sorted(out, key=lambda d: -abs(d["t_stat"]))

    def sweep_profile(self) -> List[Dict[str, object]]:
        """When do liquidity sweeps actually cluster?

        Sweeps are the fuel for the reversal lenses. Knowing they concentrate in
        specific windows (typically session opens, where the prior range's
        resting stops get taken) turns a generic pattern into a scheduled one.
        """
        out = []
        for h, c in sorted(self.by_hour.items()):
            if c.n < 40:
                continue
            rate = (c.sweeps_high + c.sweeps_low) / c.n
            out.append({"hour": h, "n": c.n, "sweep_rate": round(rate, 4),
                        "high_bias": round(
                            (c.sweeps_high - c.sweeps_low) /
                            max(c.sweeps_high + c.sweeps_low, 1), 3)})
        out.sort(key=lambda d: -d["sweep_rate"])
        return out

    def session_table(self) -> List[Dict[str, object]]:
        return [c.to_dict() for c in
                sorted(self.by_session.values(), key=lambda c: -c.n)]

    def report(self) -> Dict[str, object]:
        self.auto_split()
        sig = self.significant_hours()
        return {
            "bars_observed": self._total,
            "volatility_profile_by_hour": self.volatility_profile(),
            "significant_hours": sig,
            "sessions": self.session_table(),
            "sweep_clustering": self.sweep_profile()[:6],
            "honesty_note": (
                "Directional hour effects are reported ONLY if they survive "
                "Benjamini-Hochberg correction across all 24 buckets AND remain "
                "consistent across both halves of the sample. The volatility "
                "profile is far more reliable than any directional claim and is "
                "the part you should actually trade off."),
        }

    def features_at(self, t_ns: int) -> Dict[str, float]:
        """Point-in-time seasonal features for the FeatureFrame.

        Exposed as multipliers relative to the daily average, so a lens can say
        'only when expected volatility is above normal' without hardcoding hours.
        """
        dt = ns_to_dt(t_ns)
        prof = self.volatility_profile()
        h = dt.hour
        cell = self.by_hour.get(h)
        return {
            "seasonal_vol_mult": prof.get(h, 1.0),
            "seasonal_up_rate": round(cell.up_rate, 4) if cell and cell.n >= 30 else 0.5,
            "seasonal_sweep_rate": round(
                (cell.sweeps_high + cell.sweeps_low) / cell.n, 4)
            if cell and cell.n >= 30 else 0.0,
            "session_pos": round((dt.minute + dt.hour * 60) / 1440.0, 4),
        }
