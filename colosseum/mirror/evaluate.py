"""MIRROR — measuring what a channel ACTUALLY did.

THIS IS THE FIRST THING TO BUILD AND THE MOST IMPORTANT.

Before spending a single hour trying to clone a channel's logic, you have to
answer one question honestly: **is it actually profitable?** Not "does the
channel say it is." Not "does the win rate in the pinned message look good."
Does replaying every signal it posted, against YOUR OWN tick data, with YOUR
spread and slippage, produce money.

Most published signal-channel records are unreliable for structural reasons that
have nothing to do with dishonesty:

  * SURVIVORSHIP — losing calls get deleted; you only ever count what remains
  * SELECTIVE REPORTING — "TP1 hit ✅" is posted; the ones that reversed are not
  * BREAK-EVEN LAUNDERING — an SL moved to entry after the fact converts a loss
    into a "BE, no loss" and quietly disappears from the stats
  * NO COST MODEL — quoted in raw pips, ignoring spread, swap, and slippage
  * FLOATING LOSERS — a position 300 pips underwater is "still running", not
    counted as a loss, sometimes for weeks
  * MARTINGALE — adding to losers looks brilliant until the one time it doesn't

This module measures against the tape, honestly, and reports the number that
matters: expectancy per signal, net of cost, counting everything.

If the answer is "not profitable", you have saved yourself the entire cloning
project — and that is a genuinely valuable outcome, not a failed one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.types import Bar
from .parse import ParsedSignal, ParsedUpdate


@dataclass
class TradeOutcome:
    msg_id: str
    filled: bool
    t_fill_ns: int = 0
    t_exit_ns: int = 0
    exit_reason: str = ""          # tp1..tpN | sl | be | timeout | unfilled | open
    r_gross: float = 0.0
    r_net: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    tps_hit: int = 0
    bars_held: int = 0
    went_underwater_r: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {"msg_id": self.msg_id, "filled": self.filled,
                "exit_reason": self.exit_reason,
                "r_gross": round(self.r_gross, 3), "r_net": round(self.r_net, 3),
                "mfe_r": round(self.mfe_r, 3), "mae_r": round(self.mae_r, 3),
                "tps_hit": self.tps_hit, "bars_held": self.bars_held}


@dataclass
class CostModel:
    """Be pessimistic here. Optimistic cost assumptions are the single most
    common way a signal record looks profitable and isn't."""
    spread: float = 0.30           # XAUUSD price units
    slippage_entry: float = 0.10
    slippage_exit: float = 0.15    # exits are worse: stops fill in fast markets
    commission: float = 0.10

    @property
    def round_trip(self) -> float:
        return (self.spread + self.slippage_entry + self.slippage_exit
                + self.commission)


def replay_signal(sig: ParsedSignal, bars: Sequence[Bar],
                  cost: CostModel,
                  updates: Optional[Sequence[ParsedUpdate]] = None,
                  fill_window_bars: int = 48,
                  max_hold_bars: int = 480,
                  apply_be_moves: bool = True) -> TradeOutcome:
    """Replay one signal against real bars.

    Conventions, all chosen to be CONSERVATIVE, because a generous replay is
    just a slower way of lying to yourself:
      * a limit entry needs the bar to actually trade through it
      * if a bar spans both stop and target, the STOP is assumed first
      * costs are charged on entry and exit
      * an unfilled signal is recorded, not discarded
    """
    out = TradeOutcome(msg_id=sig.msg_id, filled=False)
    if sig.stop is None or not bars:
        out.exit_reason = "unparseable_levels"
        return out

    long_ = sig.direction == "long"
    entry_px = sig.entry
    lo_e = min(sig.entry, sig.entry_high) if (sig.entry and sig.entry_high) else sig.entry
    hi_e = max(sig.entry, sig.entry_high) if (sig.entry and sig.entry_high) else sig.entry

    i0 = 0
    if sig.is_market or entry_px is None:
        entry_px = bars[0].o
        out.filled = True
        out.t_fill_ns = bars[0].t_open_ns
    else:
        for i, b in enumerate(bars[:fill_window_bars]):
            if b.l <= (hi_e or entry_px) and b.h >= (lo_e or entry_px):
                out.filled = True
                out.t_fill_ns = b.t_close_ns
                i0 = i
                break
        if not out.filled:
            out.exit_reason = "unfilled"
            return out

    risk = abs(entry_px - sig.stop)
    if risk <= 0:
        out.exit_reason = "zero_risk"
        return out

    def r_at(px: float) -> float:
        d = (px - entry_px) if long_ else (entry_px - px)
        return d / risk

    stop = sig.stop
    tps = sorted(sig.targets, key=lambda t: abs(t - entry_px))
    be_moved = False
    upd_by_time = sorted(updates or [], key=lambda u: u.t_ns)

    for n, b in enumerate(bars[i0:i0 + max_hold_bars]):
        out.bars_held = n + 1

        # apply any channel updates that landed before this bar closed
        if apply_be_moves:
            for u in upd_by_time:
                if u.t_ns <= b.t_close_ns and not be_moved:
                    if u.action == "sl_to_be":
                        stop = entry_px
                        be_moved = True
                    elif u.action == "sl_move" and u.value:
                        stop = u.value
                        be_moved = True

        fav = b.h if long_ else b.l
        adv = b.l if long_ else b.h
        out.mfe_r = max(out.mfe_r, r_at(fav))
        out.mae_r = min(out.mae_r, r_at(adv))
        out.went_underwater_r = min(out.went_underwater_r, out.mae_r)

        stop_hit = (b.l <= stop) if long_ else (b.h >= stop)
        # Conservative: stop before target when a bar spans both.
        if stop_hit:
            out.t_exit_ns = b.t_close_ns
            out.r_gross = r_at(stop)
            out.exit_reason = "be" if (be_moved and abs(stop - entry_px) < 1e-9) else "sl"
            break

        hit_any = False
        for k, tp in enumerate(tps):
            reached = (b.h >= tp) if long_ else (b.l <= tp)
            if reached and k >= out.tps_hit:
                out.tps_hit = k + 1
                hit_any = True
        if out.tps_hit >= len(tps) and tps:
            out.t_exit_ns = b.t_close_ns
            out.r_gross = r_at(tps[-1])
            out.exit_reason = f"tp{out.tps_hit}"
            break
    else:
        last = bars[min(i0 + max_hold_bars, len(bars)) - 1]
        out.t_exit_ns = last.t_close_ns
        out.r_gross = r_at(last.c)
        out.exit_reason = "timeout"

    if not out.exit_reason:
        out.exit_reason = "open"
    out.r_net = out.r_gross - (cost.round_trip / risk)
    return out


@dataclass
class ChannelReport:
    name: str
    n_signals: int
    n_filled: int
    n_wins: int
    win_rate: float
    expectancy_r: float
    total_r: float
    profit_factor: float
    max_dd_r: float
    sharpe: float
    unfilled_rate: float
    be_rate: float
    mean_mae_r: float
    tp_hit_distribution: Dict[str, int]
    verdict: str
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def evaluate_channel(name: str, outcomes: Sequence[TradeOutcome],
                     cost: CostModel) -> ChannelReport:
    """The honest scorecard."""
    n = len(outcomes)
    filled = [o for o in outcomes if o.filled]
    nf = len(filled)
    if nf == 0:
        return ChannelReport(name, n, 0, 0, 0, 0, 0, 0, 0, 0, 1.0, 0, 0, {},
                             "no filled signals to evaluate")

    rs = [o.r_net for o in filled]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    total = sum(rs)
    exp = total / nf
    gross_win = sum(wins)
    gross_loss = abs(sum(losses)) or 1e-9
    pf = gross_win / gross_loss

    peak = cum = 0.0
    dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)

    m = total / nf
    var = sum((r - m) ** 2 for r in rs) / (nf - 1) if nf > 1 else 0.0
    sharpe = m / math.sqrt(var) if var > 0 else 0.0

    dist: Dict[str, int] = {}
    for o in filled:
        dist[o.exit_reason] = dist.get(o.exit_reason, 0) + 1

    be = sum(1 for o in filled if o.exit_reason == "be")
    warnings: List[str] = []
    if be / nf > 0.15:
        warnings.append(
            f"{be/nf:.0%} of trades exited at break-even. BE moves flatter a "
            f"record dramatically — most published win rates silently assume "
            f"them. Check whether the BE instruction was posted in time to be "
            f"actionable, or only after the fact.")
    unf = 1.0 - nf / n if n else 0.0
    if unf > 0.25:
        warnings.append(
            f"{unf:.0%} of signals never filled. If the channel counts only "
            f"filled trades, its published stats are conditioned on entry — "
            f"which is a real selection effect.")
    mae = sum(o.mae_r for o in filled) / nf
    if mae < -1.5:
        warnings.append(
            f"mean MAE {mae:.2f}R — winners routinely go deeply underwater "
            f"before working. That is the signature of wide/absent stops or "
            f"averaging down, and it hides tail risk that this replay's "
            f"max-hold window may be truncating.")
    if pf > 3.0 and exp > 0.5:
        warnings.append(
            "results look very strong. Before believing them, verify message "
            "coverage — deleted or edited messages are invisible here, and "
            "survivorship is the single most common reason a channel's record "
            "looks better than its trading.")

    if exp > 0.10 and pf > 1.3:
        verdict = (f"PROFITABLE on this sample: {exp:+.3f}R expectancy per "
                   f"signal net of {cost.round_trip:.2f} cost, PF {pf:.2f} over "
                   f"{nf} filled trades. Worth reverse-engineering.")
    elif exp > 0:
        verdict = (f"MARGINAL: {exp:+.3f}R expectancy, PF {pf:.2f}. Positive but "
                   f"thin — could easily be sample luck. Needs more data before "
                   f"investing effort in cloning it.")
    else:
        verdict = (f"NOT PROFITABLE as posted: {exp:+.3f}R per signal after "
                   f"costs. Whatever the channel claims, replaying its own "
                   f"signals against real tape does not make money. Do not "
                   f"clone this — but DO study it as a negative example.")

    return ChannelReport(
        name, n, nf, len(wins), len(wins) / nf, exp, total, pf, abs(dd), sharpe,
        unf, be / nf, mae, dist, verdict, warnings)


def detect_martingale(signals: Sequence[ParsedSignal],
                      window_ns: int = 4 * 3600 * 10**9) -> Dict[str, object]:
    """Are they averaging down?

    A cluster of same-direction signals at progressively worse prices is a grid
    or martingale. Those show beautiful win rates right up until the one time
    they don't, and evaluating them per-signal massively understates the risk —
    the real position is the whole cluster, held at once.
    """
    clusters: List[List[ParsedSignal]] = []
    ordered = sorted([s for s in signals if s.entry], key=lambda s: s.t_ns)
    for s in ordered:
        placed = False
        for c in clusters:
            if (c[-1].direction == s.direction
                    and s.t_ns - c[-1].t_ns <= window_ns
                    and c[-1].symbol == s.symbol):
                worse = ((s.direction == "long" and s.entry < c[-1].entry)
                         or (s.direction == "short" and s.entry > c[-1].entry))
                if worse:
                    c.append(s)
                    placed = True
                    break
        if not placed:
            clusters.append([s])
    grids = [c for c in clusters if len(c) >= 3]
    return {
        "clusters_found": len(grids),
        "max_cluster_size": max((len(c) for c in grids), default=0),
        "share_of_signals_in_grids": round(
            sum(len(c) for c in grids) / max(len(ordered), 1), 4),
        "assessment": (
            "Grid/martingale behaviour detected. Per-signal statistics "
            "UNDERSTATE risk here: the real exposure is the whole cluster held "
            "simultaneously. Evaluate cluster-level drawdown before believing "
            "any win rate." if grids else
            "No systematic averaging-down detected — signals look independent."),
    }
