"""The Arbiter: the tournament official that decides what actually publishes.

Not a strategist -- it never forms a market view. It enforces the rules that
keep the ensemble honest:

  * cost gate      -- a signal whose TP1 doesn't clear spread+commission by a
                      required multiple is not a trade, it's a donation
  * correlation cap-- five longs at the same level in the same hour is ONE
                      leveraged bet wearing five hats, not five signals
  * staleness gate -- a proposal arriving after its frame's deadline is
                      reasoning about tape that has already moved
  * shelf weighting-- rank decides who publishes at full conviction
  * confluence     -- independent agreement is upgraded; disagreement is
                      published as a decision point, not silently dropped
  * honest throttle-- the >=5/day target is a 7-day mean, never a daily quota
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..core.types import FeatureFrame, Proposal, Side


@dataclass
class CostModel:
    """Signals must be net of cost or the learner trains on fantasy."""
    spread: float = 0.30           # typical XAUUSD spread in price units
    commission_per_unit: float = 0.05
    slippage: float = 0.10
    min_edge_multiple: float = 2.5  # TP1 must clear cost by at least this much

    @property
    def round_trip(self) -> float:
        return self.spread + self.slippage + 2 * self.commission_per_unit

    def edge_after_cost(self, p: Proposal) -> float:
        tp1 = min(p.targets, key=lambda t: abs(t.price - p.entry))
        gross = abs(tp1.price - p.entry)
        return gross - self.round_trip

    def clears(self, p: Proposal) -> Tuple[bool, float, str]:
        edge = self.edge_after_cost(p)
        need = self.round_trip * self.min_edge_multiple
        gross = abs(min(p.targets, key=lambda t: abs(t.price - p.entry)).price - p.entry)
        if gross < need:
            return False, edge, (f"TP1 gross {gross:.2f} < required "
                                 f"{need:.2f} (cost {self.round_trip:.2f} x "
                                 f"{self.min_edge_multiple}) -- cost eats the edge")
        return True, edge, ""


@dataclass
class ArbiterConfig:
    max_concurrent: int = 4
    max_same_direction: int = 3
    min_level_separation_atr: float = 0.75
    min_seconds_between_same_seat: int = 300
    correlation_window_s: int = 3600
    target_signals_per_day: float = 5.0


@dataclass
class Published:
    proposal: Proposal
    conviction_final: float
    confluence_with: List[str] = field(default_factory=list)
    edge_after_cost: float = 0.0
    shelf_weight: float = 0.0
    note: str = ""


@dataclass
class Rejected:
    proposal: Proposal
    reason: str
    detail: str = ""


class Arbiter:
    def __init__(self, cfg: Optional[ArbiterConfig] = None,
                 cost: Optional[CostModel] = None):
        self.cfg = cfg or ArbiterConfig()
        self.cost = cost or CostModel()
        # (t_ns, proposal, signal_id). Keyed by id so capacity can be RELEASED
        # the moment a signal resolves -- see on_resolved().
        self.open_signals: List[Tuple[int, Proposal, str]] = []
        self.recent_by_seat: Dict[str, int] = {}
        self.rejections: List[Rejected] = []
        self.published_count = 0
        # Per-session counts. Cumulative counters divided by "days elapsed" are
        # a classic silent lie: on day 30 they report 30 days of signals as if
        # they happened today.
        self.session_counts: Dict[str, int] = {}
        self.session_rejections: Dict[str, Dict[str, int]] = {}

    def adjudicate(self, frame: FeatureFrame, proposals: Sequence[Proposal],
                   shelf_weights: Dict[str, float],
                   conviction_multiplier: float = 1.0,
                   atr: float = 1.0) -> Tuple[List[Published], List[Rejected]]:
        now = frame.t_event_ns
        self._expire(now)
        out: List[Published] = []
        rej: List[Rejected] = []

        # 1. staleness + cost + shelf gates
        alive: List[Proposal] = []
        for p in proposals:
            if p.t_ns > frame.deadline_ns:
                rej.append(Rejected(p, "stale", "arrived after frame deadline "
                                    "-- reasoning about tape that already moved"))
                continue
            if shelf_weights.get(p.seat_id, 0.0) <= 0.0:
                rej.append(Rejected(p, "benched",
                                    "seat has no live shelf space; paper only"))
                continue
            last = self.recent_by_seat.get(p.seat_id, 0)
            if last and (now - last) / 1e9 < self.cfg.min_seconds_between_same_seat:
                rej.append(Rejected(p, "seat_cooldown",
                                    "same seat re-firing too soon -- either it's "
                                    "playing out or it's invalidated"))
                continue
            ok, edge, why = self.cost.clears(p)
            if not ok:
                rej.append(Rejected(p, "cost_gate", why))
                continue
            alive.append(p)

        # 2. cluster by (direction, level proximity) -> confluence vs correlation
        clusters = self._cluster(alive, atr)

        for cluster in clusters:
            cluster.sort(key=lambda p: -shelf_weights.get(p.seat_id, 0.0))
            lead = cluster[0]
            others = cluster[1:]

            if len(self.open_signals) >= self.cfg.max_concurrent:
                rej.append(Rejected(lead, "exposure_cap",
                                    "max concurrent signals reached"))
                continue
            same_dir = sum(1 for _, q, _sid in self.open_signals
                           if q.direction is lead.direction)
            if same_dir >= self.cfg.max_same_direction:
                rej.append(Rejected(lead, "correlation_cap",
                                    f"{same_dir} open {lead.direction.value} "
                                    "signals already -- this is one leveraged "
                                    "bet, not another independent one"))
                continue

            # Confluence: independent seats agreeing raises conviction, but the
            # correlated followers are NOT published as new entries.
            boost = 1.0 + 0.12 * len(others)
            conv = min(0.98, lead.conviction * boost * conviction_multiplier)
            edge = self.cost.edge_after_cost(lead)
            out.append(Published(
                proposal=lead, conviction_final=conv,
                confluence_with=[f"{o.seat_id}:{o.direction.value}" for o in others],
                edge_after_cost=edge,
                shelf_weight=shelf_weights.get(lead.seat_id, 0.0),
                note=("independent confluence from "
                      f"{len(others)} other seat(s)") if others else "single-seat"))
            for o in others:
                rej.append(Rejected(o, "confluence_follower",
                                    f"merged into {lead.seat_id}'s signal as "
                                    "confluence rather than a second position"))
            self.open_signals.append((now, lead, ""))
            self.recent_by_seat[lead.seat_id] = now
            self.published_count += 1
            sd = frame.session_date
            self.session_counts[sd] = self.session_counts.get(sd, 0) + 1

        # 3. genuine disagreement is information, not noise -- surface it
        dirs = {p.direction for p in alive}
        if len(dirs) > 1 and out:
            out[-1].note += " | seats disagree on direction -- market at a "\
                            "decision point"

        self.rejections.extend(rej)
        return out, rej

    def _cluster(self, ps: Sequence[Proposal], atr: float) -> List[List[Proposal]]:
        sep = max(self.cfg.min_level_separation_atr * atr, 1e-9)
        clusters: List[List[Proposal]] = []
        for p in ps:
            for c in clusters:
                if c[0].direction is p.direction and abs(c[0].entry - p.entry) <= sep:
                    c.append(p)
                    break
            else:
                clusters.append([p])
        return clusters

    def bind_signal_id(self, seat_id: str, t_ns: int, signal_id: str) -> None:
        """Attach the ledger ID to the most recent open slot for this seat, so
        the slot can be released by ID when the signal resolves."""
        for i in range(len(self.open_signals) - 1, -1, -1):
            t, p, sid = self.open_signals[i]
            if not sid and p.seat_id == seat_id and t == t_ns:
                self.open_signals[i] = (t, p, signal_id)
                return

    def on_resolved(self, signal_id: str) -> bool:
        """Release exposure capacity the instant a signal actually resolves.

        Without this the arbiter holds a slot for the full correlation window
        (an hour by default) even when the trade closed in five minutes. Four
        quick scalps would then lock the engine out for the rest of the hour --
        a silent, self-inflicted cap on the >=5/day target that looks exactly
        like "the market was thin."
        """
        before = len(self.open_signals)
        self.open_signals = [x for x in self.open_signals if x[2] != signal_id]
        return len(self.open_signals) < before

    def _expire(self, now: int) -> None:
        """Backstop only. Signals that never report a resolution (crash, missed
        callback) still age out, so a lost message cannot wedge capacity forever."""
        w = self.cfg.correlation_window_s * 1_000_000_000
        self.open_signals = [(t, p, s) for t, p, s in self.open_signals
                             if now - t < w]

    def throughput_report(self, days: float = 1.0,
                          session_date: Optional[str] = None) -> Dict[str, object]:
        """Honest accounting against the >=5/day goal.

        Rate is computed from PER-SESSION counts, never cumulative-over-days.
        The prescribed response to a shortfall is diagnosis -- never lowering the
        quality bar. Forcing signals is a punished failure mode, not a fix.
        """
        if session_date is not None:
            today = self.session_counts.get(session_date, 0)
            rate = float(today)
        elif self.session_counts:
            rate = sum(self.session_counts.values()) / len(self.session_counts)
        else:
            rate = self.published_count / days if days > 0 else 0.0

        by_reason: Dict[str, int] = {}
        for r in self.rejections:
            by_reason[r.reason] = by_reason.get(r.reason, 0) + 1
        short = rate < self.cfg.target_signals_per_day
        return {
            "signals_per_day": round(rate, 2),
            "target": self.cfg.target_signals_per_day,
            "meeting_target": not short,
            "sessions_measured": len(self.session_counts),
            "open_slots_held": len(self.open_signals),
            "rejections_by_reason": by_reason,
            "diagnosis": self._diagnose(by_reason) if short else "on target",
        }

    def _diagnose(self, by_reason: Dict[str, int]) -> str:
        if not by_reason:
            return ("Pre-filters are not flagging candidates: either the market "
                    "is genuinely thin or lens sensitivity is too low. Tune "
                    "SENSITIVITY, never the quality bar.")
        top = max(by_reason, key=by_reason.get)
        return {
            "cost_gate": "Targets are too close relative to spread. Either seek "
                         "wider setups or renegotiate spread -- do NOT lower the "
                         "cost multiple.",
            "stale": "Inference latency is eating signals. Tighten the "
                     "pre-filter or scale inference.",
            "seat_cooldown": "Seats re-firing the same idea. Increase lens "
                             "diversity rather than shortening the cooldown.",
            "correlation_cap": "Signals are heavily correlated -- real diversity "
                               "is low. Add a regime-appropriate lens.",
            "benched": "Too many seats benched. Roster is degraded; run "
                       "challenger promotion.",
            "exposure_cap": "Exposure cap binding -- healthy constraint, not a "
                            "shortfall to fix.",
        }.get(top, f"dominant rejection: {top}")
