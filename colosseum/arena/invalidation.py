"""Invalidation monitoring: acting on the early tell.

THE BIGGEST FREE WIN IN THE SYSTEM.

Every signal is required to state "the single observation that proves me wrong
BEFORE the stop is hit." Until now the engine wrote that down, journaled it
beautifully... and then ignored it, waiting patiently for the full stop loss.

That is leaving money on the table on every losing trade. If a strategist can
name the early tell and the tell fires, the correct action is to exit at that
moment, not to sit through the remaining distance to the stop. Across a large
sample this converts a chunk of full -1.0R losses into partial losses, which
moves expectancy more than almost any improvement to entry selection.

It also creates a new, extremely valuable training label: **was the stated
invalidation predictive?** A seat whose invalidations fire before losses is
genuinely reading the tape. A seat whose invalidations never fire, or fire on
winners, is writing invalidations as decoration. That distinction is now
measurable, and it feeds the reward function.

Invalidation conditions are parsed from natural language into machine-checkable
predicates. Anything unparseable is honestly reported as such rather than
silently treated as "never fires."
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core.types import Bar, Proposal, Side, TFView


@dataclass(frozen=True)
class Condition:
    """A machine-checkable invalidation predicate."""
    kind: str              # close_beyond | delta_expansion | vwap_side | volume_dryup | structure
    level: Optional[float] = None
    direction: Optional[str] = None    # above | below
    requires_expansion: bool = False
    source_text: str = ""

    def describe(self) -> str:
        if self.kind == "close_beyond":
            return (f"close {self.direction} {self.level:.2f}"
                    + (" on expanding delta" if self.requires_expansion else ""))
        return self.kind


@dataclass
class Watch:
    signal_id: str
    proposal: Proposal
    conditions: List[Condition]
    parsed: bool
    raw: str
    fired: bool = False
    fired_at_ns: int = 0
    fired_condition: str = ""
    r_at_fire: float = 0.0
    bars_watched: int = 0


NUM = r"(\d{3,5}(?:\.\d{1,3})?)"


def parse_invalidation(text: str, p: Proposal) -> Tuple[List[Condition], bool]:
    """Natural language -> predicates.

    Deliberately conservative: it only emits a condition it is confident about.
    A wrong predicate that exits winners early is far more expensive than a
    missing predicate that simply falls back to the stop.
    """
    if not text:
        return [], False
    t = text.lower()
    conds: List[Condition] = []
    expansion = bool(re.search(
        r"expand\w*\s+delta|expanding\s+(?:volume|delta)|on\s+volume|"
        r"with\s+(?:conviction|volume)|rising\s+delta", t))

    # "close back above 2419.2" / "1m close below 2401"
    for m in re.finditer(r"clos\w*\s+(?:back\s+)?(above|below|over|under)\s+" + NUM, t):
        d = "above" if m.group(1) in ("above", "over") else "below"
        conds.append(Condition("close_beyond", float(m.group(2)), d,
                               expansion, m.group(0)))

    # "back above 2419" without the word close -- treat as close-based anyway,
    # because acting on a wick is how you get stopped out of a good trade.
    if not conds:
        for m in re.finditer(r"(?:back\s+)?(above|below|over|under)\s+" + NUM, t):
            d = "above" if m.group(1) in ("above", "over") else "below"
            conds.append(Condition("close_beyond", float(m.group(2)), d,
                                   expansion, m.group(0)))
            break

    # "reclaims VWAP" / "loses VWAP" -- side-of-VWAP flip against the trade
    if re.search(r"reclaim\w*\s+vwap|back\s+above\s+vwap", t):
        conds.append(Condition("vwap_side", direction="above",
                               requires_expansion=expansion, source_text="reclaims vwap"))
    if re.search(r"los\w+\s+vwap|breaks?\s+below\s+vwap", t):
        conds.append(Condition("vwap_side", direction="below",
                               requires_expansion=expansion, source_text="loses vwap"))

    # structural failure
    if re.search(r"break\w*\s+of\s+structure|bos\b|new\s+(?:higher\s+high|lower\s+low)", t):
        conds.append(Condition("structure", source_text="structure break against"))

    # If a "condition" is only ever satisfied by the stop itself, it is not an
    # early tell. Drop it -- and say so.
    keep = []
    for c in conds:
        if c.kind == "close_beyond" and c.level is not None:
            if p.direction is Side.LONG and c.direction == "below" and c.level <= p.stop:
                continue
            if p.direction is Side.SHORT and c.direction == "above" and c.level >= p.stop:
                continue
        keep.append(c)

    return keep, bool(keep)


class InvalidationMonitor:
    """Watches open signals for their own stated early tells.

    Exit policy is deliberately conservative: exit on a CLOSE beyond the level,
    never on a wick, because a wick through a level is a liquidity sweep -- which
    is frequently the *opposite* signal to a genuine break and is often the
    precise moment the original thesis is about to work.
    """

    def __init__(self, *, require_expansion_default: bool = False,
                 min_bars_before_exit: int = 1):
        self.watches: Dict[str, Watch] = {}
        self.require_expansion_default = require_expansion_default
        self.min_bars_before_exit = min_bars_before_exit
        self.stats = {"watched": 0, "parsed": 0, "unparseable": 0,
                      "fired": 0, "saved_r": 0.0}

    def watch(self, signal_id: str, p: Proposal) -> Watch:
        conds, parsed = parse_invalidation(p.invalidation, p)
        w = Watch(signal_id, p, conds, parsed, p.invalidation)
        self.watches[signal_id] = w
        self.stats["watched"] += 1
        self.stats["parsed" if parsed else "unparseable"] += 1
        return w

    def release(self, signal_id: str) -> None:
        self.watches.pop(signal_id, None)

    def check(self, bar: Bar, view: Optional[TFView] = None
              ) -> List[Tuple[str, Watch]]:
        """Evaluate every open watch against a CLOSED bar.

        Returns the signals whose invalidation just fired -- the engine should
        exit them at the bar close rather than wait for the stop.
        """
        out: List[Tuple[str, Watch]] = []
        for sid, w in list(self.watches.items()):
            if w.fired or not w.conditions:
                continue
            w.bars_watched += 1
            if w.bars_watched < self.min_bars_before_exit:
                continue
            hit = self._evaluate(w, bar, view)
            if hit:
                w.fired = True
                w.fired_at_ns = bar.t_close_ns
                w.fired_condition = hit
                w.r_at_fire = w.proposal.r_multiple(bar.c)
                self.stats["fired"] += 1
                # Saved R = the distance we did NOT travel to the stop.
                self.stats["saved_r"] += max(0.0, -1.0 - w.r_at_fire) * -1.0 \
                    if w.r_at_fire > -1.0 else 0.0
                out.append((sid, w))
        return out

    def _evaluate(self, w: Watch, bar: Bar,
                  view: Optional[TFView]) -> Optional[str]:
        p = w.proposal
        expanding = self._delta_expanding(bar, view)
        for c in w.conditions:
            if c.kind == "close_beyond" and c.level is not None:
                # CLOSE, never wick. A wick through is a sweep, not a break.
                broke = (bar.c > c.level) if c.direction == "above" else (bar.c < c.level)
                if not broke:
                    continue
                # Only counts if it is against the position.
                against = ((p.direction is Side.SHORT and c.direction == "above")
                           or (p.direction is Side.LONG and c.direction == "below"))
                if not against:
                    continue
                if c.requires_expansion and not expanding:
                    continue
                return c.describe()

            if c.kind == "vwap_side" and view and view.vwap:
                v = view.vwap.vwap
                flipped = (bar.c > v) if c.direction == "above" else (bar.c < v)
                against = ((p.direction is Side.SHORT and c.direction == "above")
                           or (p.direction is Side.LONG and c.direction == "below"))
                if flipped and against and bar.closed:
                    if c.requires_expansion and not expanding:
                        continue
                    return f"price closed {c.direction} VWAP against the position"

            if c.kind == "structure" and view and view.structure_events:
                last = view.structure_events[-1][1].value
                against = ((p.direction is Side.SHORT and last.endswith("_up"))
                           or (p.direction is Side.LONG and last.endswith("_down")))
                if against:
                    return f"structure event {last} against the position"
        return None

    @staticmethod
    def _delta_expanding(bar: Bar, view: Optional[TFView]) -> bool:
        """Is aggressive flow genuinely expanding in the breaking direction?

        When no timeframe view is available, judge on delta ratio alone rather
        than defaulting rvol to 1.0 and failing the test. Defaulting to "not
        expanding" silently disabled every expansion-qualified invalidation --
        the monitor watched a clean break and did nothing.
        """
        if bar.volume <= 0:
            return False
        ratio = abs(bar.delta) / bar.volume
        if view is None or view.flow is None:
            return ratio > 0.25
        return ratio > 0.25 and view.flow.rvol > 1.1

    def report(self) -> Dict[str, object]:
        parsed = self.stats["parsed"]
        total = max(self.stats["watched"], 1)
        return {
            **self.stats,
            "parse_rate": round(parsed / total, 3),
            "note": ("Unparseable invalidations are not failures of the monitor "
                     "-- they are signals whose stated early tell was too vague "
                     "to check. That is itself a quality metric on the seat."),
        }


def invalidation_quality(fired: bool, won: bool) -> Tuple[str, float]:
    """Grade the invalidation itself -- a new, high-value learning label.

    An invalidation that fires before losses and stays quiet on winners is
    genuine tape reading. One that never fires is decoration. One that fires on
    winners is actively harmful and must be punished, because it will cut good
    trades short forever.
    """
    if fired and not won:
        return "predictive", 0.6        # correctly warned before the loss
    if fired and won:
        return "premature", -0.8        # cut a winner: worse than useless
    if not fired and won:
        return "consistent", 0.2        # stayed quiet on a winner
    return "missed", -0.3               # lost anyway, no early warning given
