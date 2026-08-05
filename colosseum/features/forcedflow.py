"""Forced flow — the structural edge, studied properly.

THE CORE IDEA. Most market participants trade because they have a view. A
minority trade because they MUST — and that minority is the only reliably
exploitable one, because their behaviour is scheduled rather than clever.

A pension fund benchmarked to the LBMA PM price must transact near that auction
whether or not the price is attractive. A futures trader holding into first
notice day must roll or take delivery. A market maker short gamma into an expiry
must hedge as spot moves. None of these people are trying to be right. They are
trying to be done. That leaves footprints in volume and volatility that repeat
on a calendar rather than on a chart pattern — and calendars are knowable in
advance.

This is edge source #3 from ALPHA.md, it is fully visible in OHLCV, and it does
not require knowing anything the market does not already know.

--------------------------------------------------------------------------
THE CANDIDATE EVENTS (hypotheses, NOT facts to be traded blind)
--------------------------------------------------------------------------

  LBMA GOLD PRICE AUCTIONS  ~10:30 and ~15:00 London
      The successors to the old London Fix, run as electronic auctions. Vast
      benchmark-linked flow prices off them. The PM auction is the more
      significant of the two because it sets the reference most contracts and
      valuations use.

  COMEX SETTLEMENT  ~13:30 New York
      The settlement window determines marks for margin. Participants who need
      to influence or capture the settlement price transact into it.

  FUTURES ROLL / FIRST NOTICE DAY  end of the month preceding a delivery month
      Anyone holding a front-month contract who does not intend to take delivery
      must roll. Gold's active months (G, J, M, Q, Z) concentrate this.

  OPTIONS EXPIRY  a few business days before the futures month
      Dealers hedging gamma near large strikes produce mechanical buying into
      dips and selling into rallies (pinning), or the reverse if positioning is
      inverted.

  MONTH / QUARTER END
      Rebalancing, NAV strikes, and benchmark-linked flows cluster.

  SESSION HANDOVERS  Asia->London, London->NY
      Liquidity steps change abruptly. The prior session's range holds resting
      stops that the incoming, larger session can reach.

--------------------------------------------------------------------------
THE METHOD — and why it is not "here are some good hours"
--------------------------------------------------------------------------

Every one of the above is a HYPOTHESIS about where to look. None is treated as
established. For each candidate window the engine measures the anomaly in
volume, realized volatility, directional drift, reversal rate and sweep rate,
against a matched baseline, then requires:

  1. an effect size that is economically meaningful, not merely detectable
  2. survival of Benjamini-Hochberg correction across ALL windows tested
  3. stability across non-overlapping halves of the sample

A window that is significant in 2024 and absent in 2025 is reported as UNSTABLE
and is not tradeable. That distinction is the entire difference between this and
the usual "gold always rises at 3pm" folklore.

Exact clock times are deliberately NOT hardcoded as tradeable constants. London
and New York shift with their own DST schedules, and venue hours change. The
engine scans a window AROUND each nominal time and reports where the anomaly
actually peaked — let the data locate the event.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from ..core.clock import NS, ns_to_dt
from ..core.types import Bar

LONDON = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")

# Active COMEX gold delivery months (G=Feb, J=Apr, M=Jun, Q=Aug, Z=Dec)
ACTIVE_MONTHS = {2, 4, 6, 8, 12}


@dataclass(frozen=True)
class EventWindow:
    """A candidate forced-flow window, expressed in LOCAL market time so DST is
    handled correctly rather than drifting twice a year."""
    name: str
    tz: str                    # "london" | "ny" | "utc"
    hour: int
    minute: int = 0
    before_min: int = 20
    after_min: int = 20
    mechanism: str = ""
    monthly_rule: str = ""     # "", "month_end", "roll", "opex"

    def contains(self, t_ns: int) -> bool:
        tz = {"london": LONDON, "ny": NY}.get(self.tz)
        dt = ns_to_dt(t_ns)
        local = dt.astimezone(tz) if tz else dt
        mins = local.hour * 60 + local.minute
        target = self.hour * 60 + self.minute
        return -self.before_min <= (mins - target) <= self.after_min

    def offset_min(self, t_ns: int) -> int:
        tz = {"london": LONDON, "ny": NY}.get(self.tz)
        dt = ns_to_dt(t_ns)
        local = dt.astimezone(tz) if tz else dt
        return (local.hour * 60 + local.minute) - (self.hour * 60 + self.minute)


CANDIDATE_WINDOWS: List[EventWindow] = [
    EventWindow("lbma_am_auction", "london", 10, 30, 25, 25,
                "Benchmark auction. Flow linked to the AM reference must "
                "transact regardless of price view."),
    EventWindow("lbma_pm_auction", "london", 15, 0, 25, 25,
                "The more significant auction — sets the reference most "
                "contracts, ETF NAVs and valuations use. Largest scheduled "
                "non-discretionary flow of the gold day."),
    EventWindow("comex_settlement", "ny", 13, 30, 20, 20,
                "Settlement window sets marks for margin. Participants who need "
                "to capture or influence the settle transact into it."),
    EventWindow("london_open", "london", 8, 0, 30, 30,
                "Liquidity steps up sharply from Asia. The Asia range holds "
                "resting stops the larger session can now reach."),
    EventWindow("ny_open", "ny", 8, 20, 25, 25,
                "COMEX floor-era open. Volume expansion and the day's second "
                "directional impulse."),
    EventWindow("asia_open", "utc", 23, 0, 30, 30,
                "Thin book. Range establishment; sweeps here are cheap to cause "
                "and frequently retraced."),
    EventWindow("month_end_pm", "london", 15, 0, 40, 40,
                "Month-end rebalancing and NAV strikes stack on top of the PM "
                "auction.", monthly_rule="month_end"),
]


@dataclass
class WindowStats:
    name: str
    n: int = 0
    sum_abs_ret: float = 0.0
    sum_ret: float = 0.0
    sum_sq_ret: float = 0.0
    sum_vol: float = 0.0
    sum_range: float = 0.0
    reversals: int = 0
    sweeps: int = 0
    up: int = 0
    peak_offset_hist: Dict[int, float] = field(default_factory=dict)
    # (t_ns, |ret|) samples, kept so stability can be computed AFTER the fact.
    #
    # THE BUG THIS REPLACES: first/second-half accumulators required knowing the
    # sample midpoint BEFORE streaming, but the midpoint is only knowable once
    # streaming has finished. The split was therefore always 0, both halves
    # stayed empty, and every window reported stability 0.00 — silently marking
    # genuinely stable effects as untradeable artifacts.
    samples: List[Tuple[int, float]] = field(default_factory=list)
    signed_samples: List[Tuple[int, float]] = field(default_factory=list)
    _cap: int = 40000

    @property
    def mean_abs(self) -> float:
        return self.sum_abs_ret / self.n if self.n else 0.0

    @property
    def mean_ret(self) -> float:
        return self.sum_ret / self.n if self.n else 0.0

    @property
    def mean_vol(self) -> float:
        return self.sum_vol / self.n if self.n else 0.0

    @property
    def std_ret(self) -> float:
        if self.n < 2:
            return 0.0
        v = (self.sum_sq_ret - self.n * self.mean_ret ** 2) / (self.n - 1)
        return math.sqrt(max(v, 0.0))

    @property
    def reversal_rate(self) -> float:
        return self.reversals / self.n if self.n else 0.0

    @property
    def sweep_rate(self) -> float:
        return self.sweeps / self.n if self.n else 0.0

    @property
    def stability(self) -> float:
        """Agreement between the two halves of the sample, split at the median
        timestamp — computed here, once all data is in, rather than requiring
        the midpoint to be known in advance."""
        if len(self.samples) < 20:
            return 0.0
        ts = sorted(t for t, _ in self.samples)
        mid = ts[len(ts) // 2]
        a = [v for t, v in self.samples if t < mid]
        b = [v for t, v in self.samples if t >= mid]
        if len(a) < 10 or len(b) < 10:
            return 0.0
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        if ma <= 0 or mb <= 0:
            return 0.0
        return min(ma, mb) / max(ma, mb)

    def directional_stability(self) -> float:
        """Same split, but on SIGNED drift. A directional effect that flips sign
        between halves scores 0 — which is exactly how seasonal folklore dies."""
        if len(self.samples) < 20:
            return 0.0
        ts = sorted(t for t, _ in self.samples)
        mid = ts[len(ts) // 2]
        a = [v for t, v in self.signed_samples if t < mid]
        b = [v for t, v in self.signed_samples if t >= mid]
        if len(a) < 10 or len(b) < 10:
            return 0.0
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        if ma * mb <= 0:
            return 0.0
        return min(abs(ma), abs(mb)) / max(abs(ma), abs(mb))

    def peak_minute(self) -> Optional[int]:
        """Where inside the window does activity actually peak?

        This is how the engine LOCATES an event rather than trusting a nominal
        clock time — venue hours change and DST shifts; the volume does not lie.
        """
        if not self.peak_offset_hist:
            return None
        return max(self.peak_offset_hist, key=self.peak_offset_hist.get)


def _norm_sf(z: float) -> float:
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))


def _bh(pvals: Sequence[float], fdr: float = 0.10) -> List[bool]:
    n = len(pvals)
    if not n:
        return []
    order = sorted(range(n), key=lambda i: pvals[i])
    kmax = -1
    for rank, i in enumerate(order, 1):
        if pvals[i] <= fdr * rank / n:
            kmax = rank
    flags = [False] * n
    if kmax > 0:
        for rank, i in enumerate(order, 1):
            if rank <= kmax:
                flags[i] = True
    return flags


class ForcedFlowEngine:
    """Measures each candidate window against a matched baseline."""

    def __init__(self, windows: Optional[Sequence[EventWindow]] = None):
        self.windows = list(windows or CANDIDATE_WINDOWS)
        self.stats: Dict[str, WindowStats] = {
            w.name: WindowStats(w.name) for w in self.windows}
        self.baseline = WindowStats("baseline")
        self._t_min = 0
        self._t_max = 0
        self._split = 0

    @staticmethod
    def _is_month_end(t_ns: int) -> bool:
        dt = ns_to_dt(t_ns)
        nxt = dt.replace(day=28) + __import__("datetime").timedelta(days=4)
        last = (nxt - __import__("datetime").timedelta(days=nxt.day)).day
        return dt.day >= last - 1

    @staticmethod
    def _is_roll_window(t_ns: int) -> bool:
        """Approximate first-notice-day pressure: the last few sessions of the
        month preceding an active delivery month."""
        dt = ns_to_dt(t_ns)
        nxt_month = 1 if dt.month == 12 else dt.month + 1
        if nxt_month not in ACTIVE_MONTHS:
            return False
        d = dt.replace(day=28) + __import__("datetime").timedelta(days=4)
        last = (d - __import__("datetime").timedelta(days=d.day)).day
        return dt.day >= last - 4

    def update(self, b: Bar, swept: Optional[str] = None,
               reversed_: Optional[bool] = None) -> None:
        if not b.closed or b.o <= 0:
            return
        ret = (b.c - b.o) / b.o
        self._t_min = self._t_min or b.t_open_ns
        self._t_max = max(self._t_max, b.t_open_ns)
        split = self._split

        def _acc(st: WindowStats, offset: Optional[int] = None) -> None:
            st.n += 1
            st.sum_abs_ret += abs(ret)
            st.sum_ret += ret
            st.sum_sq_ret += ret * ret
            st.sum_vol += b.volume
            st.sum_range += (b.h - b.l) / b.o
            st.up += 1 if b.c >= b.o else 0
            if swept:
                st.sweeps += 1
            if reversed_:
                st.reversals += 1
            if len(st.samples) < st._cap:
                st.samples.append((b.t_open_ns, abs(ret)))
                st.signed_samples.append((b.t_open_ns, ret))
            if offset is not None:
                st.peak_offset_hist[offset] = (
                    st.peak_offset_hist.get(offset, 0.0) + abs(ret))

        in_any = False
        for w in self.windows:
            if not w.contains(b.t_open_ns):
                continue
            if w.monthly_rule == "month_end" and not self._is_month_end(b.t_open_ns):
                continue
            if w.monthly_rule == "roll" and not self._is_roll_window(b.t_open_ns):
                continue
            in_any = True
            _acc(self.stats[w.name], w.offset_min(b.t_open_ns))
        if not in_any:
            _acc(self.baseline)

    def set_split(self, t_ns: Optional[int] = None) -> None:
        self._split = t_ns or ((self._t_min + self._t_max) // 2
                               if self._t_min and self._t_max else 0)

    # ---- analysis --------------------------------------------------------

    def analyze(self, fdr: float = 0.10, min_n: int = 60,
                min_stability: float = 0.5,
                min_vol_ratio: float = 1.15) -> List[Dict[str, object]]:
        """Which windows show a REAL anomaly?

        Volatility/volume anomalies are tested primarily because they are far
        more stable than directional drift and are directly actionable (stop and
        target sizing). Directional claims are reported but held to a stricter
        bar, because that is where folklore lives.
        """
        base = self.baseline
        if base.n < min_n:
            return []
        b_abs = base.mean_abs or 1e-12
        b_vol = base.mean_vol or 1e-12
        b_sweep = base.sweep_rate

        cand = [(w, self.stats[w.name]) for w in self.windows
                if self.stats[w.name].n >= min_n]
        if not cand:
            return []

        # p-values for the DIRECTIONAL claim only (the multiple-testing hazard)
        pvals = []
        for _, st in cand:
            s = st.std_ret
            z = (st.mean_ret / (s / math.sqrt(st.n))) if (s > 0 and st.n > 2) else 0.0
            pvals.append(_norm_sf(z))
        flags = _bh(pvals, fdr)

        out = []
        for (w, st), sig, p in zip(cand, flags, pvals):
            vol_ratio = st.mean_abs / b_abs
            volume_ratio = st.mean_vol / b_vol
            stable = st.stability >= min_stability
            peak = st.peak_minute()

            findings: List[str] = []
            positive: List[str] = []   # only these justify "tradeable"
            if vol_ratio >= min_vol_ratio and stable:
                positive.append(
                    f"realized volatility {vol_ratio:.2f}x baseline — size stops "
                    f"and targets for THIS, not for the daily average")
            elif vol_ratio <= 1 / min_vol_ratio and stable:
                positive.append(
                    f"realized volatility {vol_ratio:.2f}x baseline — a quiet "
                    f"window; targets that work elsewhere will not be reached")
            if volume_ratio >= min_vol_ratio:
                positive.append(f"volume {volume_ratio:.2f}x baseline — "
                                f"participation genuinely steps up here")
            if b_sweep > 0 and st.sweep_rate > b_sweep * 1.3:
                positive.append(
                    f"liquidity sweeps {st.sweep_rate/b_sweep:.2f}x more "
                    f"frequent — resting stops are being reached in this window")
            dir_stable = st.directional_stability() >= min_stability
            if sig and dir_stable:
                positive.append(
                    f"DIRECTIONAL drift survives FDR correction and is stable "
                    f"({st.mean_ret*10000:+.2f} bp/bar). Treat with suspicion "
                    f"anyway and re-test forward.")
            elif sig and not dir_stable:
                findings.append(
                    f"directional drift is statistically significant but "
                    f"UNSTABLE across halves (dir-stability "
                    f"{st.directional_stability():.2f}) — "
                    f"this is a fitted artifact, do not trade it")

            out.append({
                "window": w.name,
                "mechanism": w.mechanism,
                "n_bars": st.n,
                "vol_ratio": round(vol_ratio, 3),
                "volume_ratio": round(volume_ratio, 3),
                "mean_ret_bp": round(st.mean_ret * 10000, 3),
                "up_rate": round(st.up / st.n, 4) if st.n else 0,
                "sweep_rate": round(st.sweep_rate, 4),
                "reversal_rate": round(st.reversal_rate, 4),
                "stability": round(st.stability, 3),
                "directional_stability": round(st.directional_stability(), 3),
                "p_value": round(p, 5),
                "directional_significant": sig,
                "peak_offset_min": peak,
                "peak_note": (
                    f"activity peaks {peak:+d} min from the nominal time — "
                    f"the engine located the event from the data rather than "
                    f"trusting the clock" if peak is not None else ""),
                "findings": positive + findings,
                # A WARNING IS NOT A FINDING. Previously any entry in `findings`
                # made a window "tradeable" — including the note saying its
                # directional drift was an unstable artifact. A window is
                # tradeable only on a POSITIVE, stable effect.
                "tradeable": bool(positive) and stable,
            })
        out.sort(key=lambda d: -d["vol_ratio"])
        return out

    def volatility_multipliers(self, min_n: int = 60) -> Dict[str, float]:
        """The most directly useful output: how much to scale stop/target sizing
        inside each window. Feeds straight into the bandit's ATR multipliers."""
        b = self.baseline.mean_abs or 1e-12
        return {name: round(st.mean_abs / b, 3)
                for name, st in self.stats.items() if st.n >= min_n}

    def features_at(self, t_ns: int) -> Dict[str, float]:
        """Point-in-time flags for the FeatureFrame, so a lens can condition on
        'we are inside the PM auction window' without hardcoding a clock time."""
        f: Dict[str, float] = {}
        mult = 1.0
        for w in self.windows:
            inside = w.contains(t_ns)
            if w.monthly_rule == "month_end" and inside:
                inside = self._is_month_end(t_ns)
            f[f"ff_{w.name}"] = 1.0 if inside else 0.0
            if inside:
                st = self.stats[w.name]
                if st.n >= 60 and self.baseline.mean_abs > 0:
                    mult = max(mult, st.mean_abs / self.baseline.mean_abs)
        f["ff_vol_mult"] = round(mult, 3)
        f["ff_in_event"] = 1.0 if any(
            v for k, v in f.items() if k.startswith("ff_") and k != "ff_vol_mult"
        ) else 0.0
        return f

    def report(self, fdr: float = 0.10) -> Dict[str, object]:
        self.set_split()
        an = self.analyze(fdr)
        tradeable = [a for a in an if a["tradeable"]]
        return {
            "baseline_bars": self.baseline.n,
            "windows_tested": len(an),
            "tradeable_windows": len(tradeable),
            "volatility_multipliers": self.volatility_multipliers(),
            "windows": an,
            "how_to_read_this": (
                "Volatility and volume ratios are the reliable outputs — they "
                "are stable, mechanically caused, and immediately useful for "
                "sizing stops and targets. DIRECTIONAL claims are held to a much "
                "stricter bar (FDR-corrected AND stable across halves) because "
                "that is exactly where seasonal folklore comes from. A window "
                "flagged UNSTABLE is a fitted artifact, not an edge."),
        }
