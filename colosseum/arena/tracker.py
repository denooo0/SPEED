"""Outcome tracker: grading every prediction against reality.

Grades EVERY signal -- published or not, taken by the human or not. That is what
makes the engine learn from its whole experience rather than only from the
trades that happened to be acted on.

Records the realized micro-event PATH, not just the destination, so `path_match`
can answer the question that actually matters: did it understand the route, or
did it get lucky with a story attached?
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core.types import Bar, Outcome, Proposal, Side
from ..learn.pathmatch import path_match


@dataclass
class _Live:
    proposal: Proposal
    entered: bool = False
    t_entry_ns: int = 0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    path: List[str] = field(default_factory=list)
    invalidated_first: bool = False
    hit_tp1: bool = False
    peak_price: float = 0.0
    trough_price: float = 0.0


class OutcomeTracker:
    def __init__(self, timeout_mult: float = 3.0, cost_r: float = 0.0):
        self.live: Dict[str, _Live] = {}
        self.timeout_mult = timeout_mult
        self.cost_r = cost_r          # cost expressed in R, subtracted from result

    def open(self, signal_id: str, p: Proposal) -> None:
        self.live[signal_id] = _Live(proposal=p, peak_price=p.entry,
                                     trough_price=p.entry)

    def on_bar(self, bar: Bar, events: Optional[List[str]] = None
               ) -> List[Tuple[str, Outcome]]:
        """Advance all open signals. Returns those that resolved on this bar."""
        done: List[Tuple[str, Outcome]] = []
        for sid, lv in list(self.live.items()):
            p = lv.proposal
            for e in (events or []):
                if not lv.path or lv.path[-1] != e:
                    lv.path.append(e)

            if not lv.entered:
                touched = (bar.l <= p.entry <= bar.h)
                if touched:
                    lv.entered = True
                    lv.t_entry_ns = bar.t_close_ns
                    lv.path.append("retest")
                else:
                    # Never filled within the horizon -> a real outcome, and an
                    # important one: entry placement was wrong.
                    horizon_ns = p.horizon_min * 60 * 1_000_000_000
                    if bar.t_close_ns - p.t_ns > horizon_ns * self.timeout_mult:
                        done.append((sid, self._resolve(
                            lv, "never_filled", bar.t_close_ns, 0.0)))
                        del self.live[sid]
                    continue

            lv.peak_price = max(lv.peak_price, bar.h)
            lv.trough_price = min(lv.trough_price, bar.l)
            fav = bar.h if p.direction is Side.LONG else bar.l
            adv = bar.l if p.direction is Side.LONG else bar.h
            lv.mfe_r = max(lv.mfe_r, p.r_multiple(fav))
            lv.mae_r = min(lv.mae_r, p.r_multiple(adv))

            stop_hit = (bar.l <= p.stop) if p.direction is Side.LONG else (bar.h >= p.stop)
            tps = sorted(p.targets,
                         key=lambda t: abs(t.price - p.entry))
            tp1, tp2 = tps[0], (tps[1] if len(tps) > 1 else tps[0])
            tp1_hit = (bar.h >= tp1.price) if p.direction is Side.LONG else (bar.l <= tp1.price)
            tp2_hit = (bar.h >= tp2.price) if p.direction is Side.LONG else (bar.l <= tp2.price)

            # Conservative tie-break: if a bar spans both the stop and a target,
            # assume the STOP filled first. Optimistic resolution here is how a
            # backtest quietly inflates every statistic downstream.
            if stop_hit:
                lv.path.append("stop")
                done.append((sid, self._resolve(lv, "stop", bar.t_close_ns, -1.0)))
                del self.live[sid]
                continue
            if tp2_hit:
                lv.path.append("target")
                done.append((sid, self._resolve(lv, "tp2", bar.t_close_ns,
                                                p.r_multiple(tp2.price))))
                del self.live[sid]
                continue
            if tp1_hit and not lv.hit_tp1:
                lv.hit_tp1 = True
                lv.path.append("target")

            horizon_ns = p.horizon_min * 60 * 1_000_000_000
            if bar.t_close_ns - lv.t_entry_ns > horizon_ns * self.timeout_mult:
                r = p.r_multiple(bar.c)
                res = "tp1" if lv.hit_tp1 else "timeout"
                done.append((sid, self._resolve(lv, res, bar.t_close_ns,
                                                p.r_multiple(tp1.price) if lv.hit_tp1 else r)))
                del self.live[sid]
        return done

    def _resolve(self, lv: _Live, resolution: str, t_ns: int,
                 gross_r: float) -> Outcome:
        p = lv.proposal
        pm = path_match(p.predicted_path, lv.path)
        net_r = gross_r - (self.cost_r if lv.entered else 0.0)

        # Mechanism verdict from route agreement. Graded SEPARATELY from profit
        # on purpose: a win whose stated mechanism never occurred teaches the
        # engine nothing and must not be allowed to look like skill.
        score = float(pm["score"])
        if score >= 0.7:
            verdict = "confirmed"
        elif score >= 0.4:
            verdict = "partial"
        else:
            verdict = "false"

        return Outcome(
            resolution=resolution, t_resolved_ns=t_ns,
            mfe_r=round(lv.mfe_r, 3), mae_r=round(lv.mae_r, 3),
            realized_r=round(net_r, 3),
            path_realized=tuple(lv.path), path_match=score,
            mechanism_verdict=verdict,
            time_to_outcome_s=int((t_ns - p.t_ns) / 1e9),
            lesson_tags=tuple(self._lessons(p, resolution, verdict, lv)))

    @staticmethod
    def _lessons(p: Proposal, resolution: str, verdict: str,
                 lv: _Live) -> List[str]:
        """Auto-tagged lessons. These feed the lesson library, where repetition
        turns an observation into knowledge."""
        out: List[str] = []
        if resolution.startswith("tp") and verdict == "confirmed":
            out.append("mechanism_held")
        if resolution == "stop" and lv.mfe_r > 1.0:
            out.append("target_beyond_realistic_range")
        if resolution == "stop" and abs(lv.mae_r) < 1.05 and lv.mfe_r > 0.5:
            out.append("stop_too_tight_for_atr")
        if resolution == "never_filled":
            out.append("entry_offset_too_patient")
        if verdict == "false" and resolution.startswith("tp"):
            out.append("right_for_wrong_reason")
        if p.is_exploration:
            out.append("exploration_trade")
        return out

    @property
    def open_count(self) -> int:
        return len(self.live)
