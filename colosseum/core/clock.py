"""Time discipline.

Two laws, both non-negotiable:

  1. TIME IS AN INPUT, NEVER A READ. No component in the reasoning path calls
     time.time(). Wall-clock reads make replay non-deterministic, which breaks
     the entire self-healing story (Guardian repair rung 3 depends on bit-exact
     replay). The only place `now` is legitimate is infrastructure telemetry.

  2. SESSION BOUNDARIES ARE DST-AWARE. The FX/metals day rolls at 17:00
     New York, which is 21:00 UTC in summer and 22:00 UTC in winter. Hardcoding
     a UTC hour silently corrupts every session-anchored VWAP twice a year.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NS = 1_000_000_000
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

ROLL_HOUR_NY = 17  # 5pm New York = the metals/FX day boundary


def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / NS, tz=UTC)


def dt_to_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected: all time must be tz-aware UTC")
    return int(dt.timestamp() * NS)


def session_start_ns(t_ns: int) -> int:
    """Start of the trading session containing t_ns (DST-correct)."""
    dt_ny = ns_to_dt(t_ns).astimezone(NY)
    anchor = dt_ny.replace(hour=ROLL_HOUR_NY, minute=0, second=0, microsecond=0)
    if dt_ny < anchor:
        anchor -= timedelta(days=1)
    return dt_to_ns(anchor.astimezone(UTC))


def session_date(t_ns: int) -> str:
    """The session's label date (the date it *ends* on, NY convention)."""
    start = session_start_ns(t_ns)
    return (ns_to_dt(start).astimezone(NY) + timedelta(days=1)).strftime("%Y-%m-%d")


def is_weekend_gap(t_ns: int) -> bool:
    """Gold is closed Fri 17:00 NY -> Sun 17:00 NY. Never treat that as a data gap."""
    dt = ns_to_dt(t_ns).astimezone(NY)
    wd, hr = dt.weekday(), dt.hour
    if wd == 4 and hr >= ROLL_HOUR_NY:
        return True
    if wd == 5:
        return True
    if wd == 6 and hr < ROLL_HOUR_NY:
        return True
    return False


def liquidity_session(t_ns: int) -> str:
    """Coarse regime context. Asia chop and NY trend are different markets."""
    h = ns_to_dt(t_ns).hour  # UTC
    if 22 <= h or h < 7:
        return "ASIA"
    if 7 <= h < 12:
        return "LONDON"
    if 12 <= h < 17:
        return "NY_OVERLAP"
    return "NY_LATE"


def floor_ns(t_ns: int, interval_s: int) -> int:
    """Bucket a timestamp to a bar-open boundary. Pure integer math -> exact."""
    step = interval_s * NS
    return (t_ns // step) * step


@dataclass(frozen=True, slots=True)
class Clock:
    """Injected time source. Logic receives this; it never reaches for a global.

    In LIVE mode `t_ns` is advanced by the feed. In REPLAY mode it is advanced
    by the log reader. Identical code path -> identical results. That identity
    is the whole basis of deterministic recovery.
    """
    t_ns: int
    mode: str = "LIVE"  # LIVE | REPLAY

    def advance(self, t_ns: int) -> "Clock":
        if t_ns < self.t_ns:
            raise ValueError(f"time cannot move backwards: {t_ns} < {self.t_ns}")
        return Clock(t_ns=t_ns, mode=self.mode)

    @property
    def session_date(self) -> str:
        return session_date(self.t_ns)

    @property
    def session_start(self) -> int:
        return session_start_ns(self.t_ns)

    @property
    def liquidity(self) -> str:
        return liquidity_session(self.t_ns)
