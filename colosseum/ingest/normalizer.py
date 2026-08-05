"""Tick normalization: the border guard.

Everything downstream assumes clean, monotonic, sane input. This module is the
only place that assumption is enforced -- and it enforces it by *classifying*
bad data rather than dropping it silently. A dropped tick is invisible; a
quarantined tick is evidence. The learner needs the evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..core.clock import NS, is_weekend_gap
from ..core.types import Quality, Tick


@dataclass
class NormalizerConfig:
    max_spread: float = 5.00          # XAUUSD: >$5 spread = broken feed or news vacuum
    max_jump: float = 15.00           # absolute price jump between consecutive ticks
    gap_warn_s: float = 5.0           # silence beyond this is a gap (outside weekends)
    gap_critical_s: float = 60.0      # silence beyond this is a feed failure
    max_skew_ms: float = 2000.0       # broker-vs-local clock disagreement ceiling
    price_floor: float = 100.0        # sanity rails; gold is never $12 or $90,000
    price_ceiling: float = 20000.0


@dataclass
class FeedStats:
    """Live telemetry consumed by the Guardian's detectors."""
    accepted: int = 0
    quarantined: int = 0
    late: int = 0
    duplicates: int = 0
    gaps: int = 0
    critical_gaps: int = 0
    max_gap_s: float = 0.0
    skew_ms_ewma: float = 0.0
    inter_arrival_ms: List[float] = field(default_factory=list)
    reasons: dict = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def p99_inter_arrival_ms(self) -> float:
        if not self.inter_arrival_ms:
            return 0.0
        s = sorted(self.inter_arrival_ms)
        return s[min(len(s) - 1, int(len(s) * 0.99))]


class Normalizer:
    """Stateful, single-threaded, deterministic.

    Deterministic matters: replaying the same tick stream must produce the same
    accept/quarantine decisions, or the Guardian's replay verification is
    meaningless.
    """

    def __init__(self, cfg: Optional[NormalizerConfig] = None):
        self.cfg = cfg or NormalizerConfig()
        self.stats = FeedStats()
        self._last_t_ns: int = 0
        self._last_mid: Optional[float] = None
        self._last_key: Optional[Tuple[int, float, float]] = None
        self._seen_window: set = set()

    def push(self, t: Tick) -> Tuple[Optional[Tick], List[str]]:
        """Returns (accepted_tick_or_None, notes).

        A quarantined tick is returned as None but IS counted and reasoned, so
        the Guardian sees degradation before it becomes failure.
        """
        c, notes = self.cfg, []

        # -- exact duplicate: brokers resend on reconnect. Idempotent by design.
        key = (t.t_ns, t.bid, t.ask)
        if key == self._last_key or key in self._seen_window:
            self.stats.duplicates += 1
            self.stats.note("duplicate")
            return None, ["duplicate"]

        # -- out-of-order / late arrival. NEVER backfill into a sealed bar.
        if t.t_ns < self._last_t_ns:
            self.stats.late += 1
            self.stats.note("late")
            return None, [f"late_by_{(self._last_t_ns - t.t_ns)/1e6:.1f}ms"]

        # -- sanity rails
        if not (c.price_floor < t.bid < c.price_ceiling) or \
           not (c.price_floor < t.ask < c.price_ceiling):
            return self._quarantine("price_out_of_range")
        if t.ask < t.bid:
            return self._quarantine("crossed_book")
        if t.spread > c.max_spread:
            return self._quarantine("spread_too_wide")
        if t.volume < 0:
            return self._quarantine("negative_volume")

        # -- jump filter: a single bad print can poison ADL/VWAP permanently,
        #    because those are CUMULATIVE. One bad tick, corrupted all session.
        if self._last_mid is not None and abs(t.mid - self._last_mid) > c.max_jump:
            return self._quarantine("price_jump")

        # -- gap detection (weekend-aware: the market being closed is not a fault)
        if self._last_t_ns:
            gap_s = (t.t_ns - self._last_t_ns) / NS
            if gap_s > c.gap_warn_s and not is_weekend_gap(self._last_t_ns):
                self.stats.gaps += 1
                self.stats.max_gap_s = max(self.stats.max_gap_s, gap_s)
                notes.append(f"gap_{gap_s:.1f}s")
                if gap_s > c.gap_critical_s:
                    self.stats.critical_gaps += 1
                    notes.append("gap_critical")
            self.stats.inter_arrival_ms.append(gap_s * 1000.0)
            if len(self.stats.inter_arrival_ms) > 5000:
                del self.stats.inter_arrival_ms[:2500]

        # -- clock skew telemetry (broker time vs our receipt time)
        if t.t_ingest_ns:
            skew = (t.t_ingest_ns - t.t_ns) / 1e6
            a = 0.01
            self.stats.skew_ms_ewma = (1 - a) * self.stats.skew_ms_ewma + a * skew
            if abs(skew) > c.max_skew_ms:
                notes.append("clock_skew_high")

        self._last_t_ns = t.t_ns
        self._last_mid = t.mid
        self._last_key = key
        self._seen_window.add(key)
        if len(self._seen_window) > 4096:
            self._seen_window.clear()
            self._seen_window.add(key)
        self.stats.accepted += 1
        return t, notes

    def _quarantine(self, reason: str) -> Tuple[None, List[str]]:
        self.stats.quarantined += 1
        self.stats.note(reason)
        return None, [reason]
