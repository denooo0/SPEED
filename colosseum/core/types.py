"""Immutable domain types.

Everything is frozen. A record that can be mutated after creation is a record
whose history you cannot trust -- and this engine's entire learning loop rests
on trusting history. Mutation happens by constructing a new value, never by
editing an old one.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .ids import content_hash

FEATURE_SCHEMA_VERSION = "fs-3"
CODE_VERSION = "colosseum-0.1.0"


class Quality(str, Enum):
    """Provenance of a datum. The learner weights by this; tainted data cannot
    silently become training truth."""
    CLEAN = "clean"
    LATE = "late"            # arrived after its bar sealed -> excluded from that bar
    QUARANTINED = "quarantined"  # failed sanity checks
    SYNTHETIC = "synthetic"  # gap-filled; never used as a label
    REVISED = "revised"      # broker correction of an earlier print


class Health(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    SAFE = "SAFE"
    REPLAY = "REPLAY"


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class PivotKind(str, Enum):
    HIGH = "high"
    LOW = "low"


class DivKind(str, Enum):
    REG_BEAR = "regular_bearish"
    REG_BULL = "regular_bullish"
    HID_BEAR = "hidden_bearish"
    HID_BULL = "hidden_bullish"
    HARMONY_UP = "harmony_up"
    HARMONY_DOWN = "harmony_down"


class StructureEvent(str, Enum):
    BOS_UP = "bos_up"       # break of structure, trend continuation
    BOS_DOWN = "bos_down"
    CHOCH_UP = "choch_up"   # change of character, potential reversal
    CHOCH_DOWN = "choch_down"


@dataclass(frozen=True, slots=True)
class Tick:
    t_ns: int             # event time (broker), UTC epoch nanos
    bid: float
    ask: float
    volume: float         # tick-volume proxy for spot gold; contract vol for futures
    t_ingest_ns: int = 0  # local receipt time -> measures feed latency & skew
    quality: Quality = Quality.CLEAN

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class Bar:
    """A price bar. `closed` is the single most important flag in this engine:
    only closed bars may produce structural events (see repaint safety)."""
    t_open_ns: int
    t_close_ns: int
    interval_s: int
    o: float
    h: float
    l: float
    c: float
    volume: float
    ticks: int
    buy_vol: float = 0.0    # Lee-Ready style classification
    sell_vol: float = 0.0
    closed: bool = False
    quality: Quality = Quality.CLEAN

    @property
    def range(self) -> float:
        return self.h - self.l

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def body_ratio(self) -> float:
        r = self.range
        return self.body / r if r > 0 else 0.0

    @property
    def upper_wick(self) -> float:
        return self.h - max(self.o, self.c)

    @property
    def lower_wick(self) -> float:
        return min(self.o, self.c) - self.l

    @property
    def typical(self) -> float:
        return (self.h + self.l + self.c) / 3.0

    @property
    def delta(self) -> float:
        """Order-flow imbalance proxy: net aggressive buying."""
        return self.buy_vol - self.sell_vol

    @property
    def bullish(self) -> bool:
        return self.c >= self.o


@dataclass(frozen=True, slots=True)
class Pivot:
    """A confirmed swing point.

    t_ns is WHEN IT HAPPENED. t_confirmed_ns is WHEN IT BECAME KNOWABLE.
    All downstream logic keys off t_confirmed_ns. Violating that is look-ahead
    bias -- the single most common way a beautiful backtest becomes a losing
    live system.
    """
    t_ns: int
    t_confirmed_ns: int
    price: float
    kind: PivotKind
    interval_s: int
    bar_index: int


@dataclass(frozen=True, slots=True)
class Divergence:
    kind: DivKind
    t_confirmed_ns: int
    indicator: str
    p1: Tuple[int, float]   # (t_ns, price) earlier pivot
    p2: Tuple[int, float]   # (t_ns, price) later pivot
    i1: float               # indicator value at p1
    i2: float               # indicator value at p2
    strength: float         # normalized magnitude of the disagreement
    interval_s: int


@dataclass(frozen=True, slots=True)
class VwapState:
    vwap: float
    sigma: float
    upper1: float
    lower1: float
    upper2: float
    lower2: float
    dist_sigma: float       # (price - vwap) / sigma -> the mean-reversion coordinate
    cum_volume: float
    anchor_ns: int


@dataclass(frozen=True, slots=True)
class FlowState:
    adl: float
    adl_slope: float
    obv: float
    cmf: float
    rvol: float
    delta_ratio: float      # net delta / total volume, in [-1, 1]
    effort_result: float    # RVOL / normalized range -> absorption detector


@dataclass(frozen=True, slots=True)
class TFView:
    """Everything the engine knows about one timeframe at one instant."""
    interval_s: int
    last_closed: Optional[Bar]
    live: Optional[Bar]
    vwap: Optional[VwapState]
    flow: Optional[FlowState]
    rsi: Optional[float]
    atr: Optional[float]
    trend: str = "unknown"          # up | down | range | unknown
    pivots: Tuple[Pivot, ...] = ()
    divergences: Tuple[Divergence, ...] = ()
    structure_events: Tuple[Tuple[int, StructureEvent], ...] = ()


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    """The SEALED, point-in-time view. This is the only thing a strategist sees.

    Sealed means: computed exclusively from data with t <= t_event_ns, then
    frozen and hashed. A late tick never mutates a sealed frame; it opens a new
    one. This is what makes replay verification meaningful.
    """
    t_event_ns: int
    session_date: str
    liquidity_session: str
    mid: float
    spread: float
    views: Dict[int, TFView]
    regime: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = FEATURE_SCHEMA_VERSION
    code_version: str = CODE_VERSION
    quality: Quality = Quality.CLEAN
    deadline_ns: int = 0     # proposals arriving after this are stale -> rejected
    _hash: str = ""

    def sealed(self) -> "FeatureFrame":
        return replace(self, _hash=self.digest())

    def digest(self) -> str:
        return content_hash({
            "t": self.t_event_ns, "mid": self.mid, "spread": self.spread,
            "sv": self.schema_version, "cv": self.code_version,
            "q": self.quality.value,
            "v": {str(k): _view_digest(v) for k, v in sorted(self.views.items())},
        })

    @property
    def frame_hash(self) -> str:
        return self._hash or self.digest()


def _view_digest(v: TFView) -> Dict[str, Any]:
    return {
        "i": v.interval_s,
        "c": None if v.last_closed is None else [
            v.last_closed.t_open_ns, v.last_closed.o, v.last_closed.h,
            v.last_closed.l, v.last_closed.c, v.last_closed.volume],
        "vw": None if v.vwap is None else [v.vwap.vwap, v.vwap.sigma],
        "fl": None if v.flow is None else [v.flow.adl, v.flow.obv, v.flow.rvol],
        "r": v.rsi, "a": v.atr, "t": v.trend,
        "d": [d.kind.value for d in v.divergences],
    }


@dataclass(frozen=True, slots=True)
class Target:
    price: float
    label: str
    reason: str


@dataclass(frozen=True, slots=True)
class Proposal:
    """A strategist's emission. Carries the full reasoning trace by construction --
    the type system itself refuses a signal without a mechanism and an
    invalidation. You cannot build an unfalsifiable signal here."""
    seat_id: str
    lens: str
    direction: Side
    conviction: float          # 0..1 calibrated probability
    entry: float
    stop: float
    stop_reason: str
    targets: Tuple[Target, ...]
    predicted_path: str
    mechanism: str
    invalidation: str
    thesis: str
    counter_case: str
    horizon_min: int
    frame_hash: str
    t_ns: int
    is_exploration: bool = False
    prefilter_score: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.conviction <= 1.0):
            raise ValueError("conviction must be a probability in [0,1]")
        if not self.mechanism.strip():
            raise ValueError("mechanism required: a signal without a stated "
                             "mechanism is noise wearing a costume")
        if not self.invalidation.strip():
            raise ValueError("invalidation required: unfalsifiable signals "
                             "cannot teach the learner")
        if not self.targets:
            raise ValueError("at least one target required")
        d = self.direction
        if d is Side.LONG and self.stop >= self.entry:
            raise ValueError("long stop must sit below entry")
        if d is Side.SHORT and self.stop <= self.entry:
            raise ValueError("short stop must sit above entry")

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    def r_multiple(self, price: float) -> float:
        r = self.risk
        if r <= 0:
            return 0.0
        d = (price - self.entry) if self.direction is Side.LONG else (self.entry - price)
        return d / r


@dataclass(frozen=True, slots=True)
class StandDown:
    """The negative class. Without these the learner only ever sees cases where
    it acted -- textbook selection bias."""
    seat_id: str
    t_ns: int
    frame_hash: str
    prefilter_score: float
    reason: str


@dataclass(frozen=True, slots=True)
class Outcome:
    resolution: str            # tp1|tp2|stop|invalidation_first|timeout|open
    t_resolved_ns: int
    mfe_r: float
    mae_r: float
    realized_r: float          # NET of cost -- gross R is fantasy
    path_realized: Tuple[str, ...]
    path_match: float
    mechanism_verdict: str     # confirmed|partial|false
    time_to_outcome_s: int
    lesson_tags: Tuple[str, ...] = ()
