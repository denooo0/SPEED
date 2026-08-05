"""Position sizing: converting calibrated probability into bet size.

This is the most underrated multiplier in the whole system, and it is *only*
available because the engine works so hard on calibration.

Flat sizing throws away information. If the engine genuinely knows that this
setup is a 0.72 and that one is a 0.54, betting the same on both is a decision
to ignore what it knows. Over a few hundred trades, correct sizing separates a
mediocre equity curve from a good one *with identical signals*.

The Kelly criterion gives the growth-optimal fraction. For a bet paying b:1 with
win probability p:

    f* = (p(b + 1) - 1) / b        equivalently  p - (1-p)/b

Three disciplines applied here, because raw Kelly is dangerous in markets:

  1. FRACTIONAL KELLY (default 0.25). Full Kelly assumes you know p exactly. You
     don't — you have an estimate with error. Quarter-Kelly gives up ~25% of
     theoretical growth for a very large reduction in variance and drawdown, and
     it is what practitioners who survive actually use.

  2. CALIBRATION-GATED. If the seat's Brier score says its probabilities are
     unreliable, its p is shrunk toward the base rate before sizing. An
     overconfident seat cannot size itself up. This is the direct payoff of the
     whole calibration-first design.

  3. HARD CAPS + DRAWDOWN THROTTLE. Kelly is unbounded as the edge grows; real
     accounts are not. Size is capped, and it scales down as drawdown deepens.

If the sized fraction is <= 0, the honest answer is that the signal has no
positive expectancy after costs and should not be taken at all — regardless of
how good the story sounds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class SizingPolicy:
    kelly_fraction: float = 0.25      # quarter-Kelly
    max_risk_pct: float = 1.0         # max % of equity risked on one signal
    # Kelly fraction treated as "full size". With a 2:1 payoff, raw Kelly
    # exceeds any sane risk cap for almost any p above breakeven, so a
    # naive "compute Kelly then cap" saturates at the ceiling for EVERY
    # signal -- sizing that returns 1.00% no matter what has thrown away
    # the calibrated probability it was supposed to use. Scaling onto the
    # allowed band instead keeps size monotone in edge across the range
    # that actually occurs.
    kelly_reference: float = 0.35
    min_risk_pct: float = 0.10        # below this, not worth the ticket
    drawdown_throttle_start: float = 0.05   # begin scaling down at 5% DD
    drawdown_throttle_full: float = 0.20    # zero size at 20% DD
    calibration_floor: float = 0.15   # max shrink toward base rate
    max_concurrent_risk_pct: float = 3.0


@dataclass(frozen=True)
class SizeDecision:
    take: bool
    risk_pct: float
    units: float
    kelly_raw: float
    p_used: float
    p_raw: float
    payoff_b: float
    edge: float
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {"take": self.take, "risk_pct": round(self.risk_pct, 4),
                "units": round(self.units, 4),
                "kelly_raw": round(self.kelly_raw, 4),
                "p_used": round(self.p_used, 4), "p_raw": round(self.p_raw, 4),
                "payoff_b": round(self.payoff_b, 3),
                "edge": round(self.edge, 4), "reason": self.reason}


def shrink_for_calibration(p: float, brier: float, base_rate: float = 0.5,
                           floor: float = 0.15) -> float:
    """Shrink a probability toward the base rate in proportion to unreliability.

    Brier 0.25 is the coin-flip reference. A seat at or beyond that has
    demonstrated its numbers carry no information, so its p collapses to the base
    rate and it cannot size itself up on confidence it has not earned.
    """
    reliability = max(0.0, min(1.0, (0.25 - brier) / 0.25))
    w = max(floor, reliability)
    return base_rate + w * (p - base_rate)


def kelly_fraction(p: float, b: float) -> float:
    """f* = p - (1-p)/b. Negative means no edge -- do not take the bet."""
    if b <= 0:
        return 0.0
    return p - (1.0 - p) / b


def size_signal(*, p_win: float, r_multiple: float, cost_r: float = 0.0,
                brier: float = 0.20, base_rate: float = 0.5,
                equity: float = 10_000.0, risk_per_r: float = 100.0,
                open_risk_pct: float = 0.0, drawdown: float = 0.0,
                policy: Optional[SizingPolicy] = None) -> SizeDecision:
    """Full sizing decision.

    `r_multiple` is the payoff at target in R. `cost_r` is round-trip cost
    expressed in R and is subtracted from the payoff BEFORE Kelly runs -- sizing
    on gross payoff is how accounts die slowly while the backtest smiles.
    """
    pol = policy or SizingPolicy()

    b = max(r_multiple - cost_r, 0.0)
    if b <= 0:
        return SizeDecision(False, 0.0, 0.0, 0.0, p_win, p_win, b, 0.0,
                            "payoff after cost is zero or negative — this is "
                            "not a trade, it is a donation")

    p_used = shrink_for_calibration(p_win, brier, base_rate,
                                    pol.calibration_floor)
    f = kelly_fraction(p_used, b)
    edge = p_used * b - (1 - p_used)

    if f <= 0:
        return SizeDecision(False, 0.0, 0.0, f, p_used, p_win, b, edge,
                            f"negative Kelly (p={p_used:.2f}, b={b:.2f}) — no "
                            f"positive expectancy after costs, regardless of "
                            f"how good the thesis reads")

    # Scale Kelly onto the permitted risk band, so size stays monotone in edge
    # instead of pinning to the ceiling.
    scaled = min(1.0, (f * pol.kelly_fraction) /
                 max(pol.kelly_reference * pol.kelly_fraction, 1e-9))
    risk_pct = pol.max_risk_pct * scaled

    # Cap BEFORE the throttle. Applying the cap last silently swallowed the
    # drawdown taper: a raw size scaled down by a deep drawdown was then capped
    # back UP to the ceiling, so the throttle did nothing at exactly the moment
    # it mattered most.
    risk_pct = min(risk_pct, pol.max_risk_pct)

    # drawdown throttle: linear taper to zero
    if drawdown > pol.drawdown_throttle_start:
        span = pol.drawdown_throttle_full - pol.drawdown_throttle_start
        scale = max(0.0, 1.0 - (drawdown - pol.drawdown_throttle_start)
                    / max(span, 1e-9))
        risk_pct *= scale
        if scale == 0.0:
            return SizeDecision(False, 0.0, 0.0, f, p_used, p_win, b, edge,
                                f"drawdown {drawdown:.1%} at or beyond the "
                                f"{pol.drawdown_throttle_full:.0%} limit — "
                                f"sizing to zero")

    headroom = pol.max_concurrent_risk_pct - open_risk_pct
    if headroom <= 0:
        return SizeDecision(False, 0.0, 0.0, f, p_used, p_win, b, edge,
                            f"portfolio risk budget full "
                            f"({open_risk_pct:.2f}% already at risk)")
    risk_pct = min(risk_pct, headroom)

    if risk_pct < pol.min_risk_pct:
        return SizeDecision(False, 0.0, 0.0, f, p_used, p_win, b, edge,
                            f"sized risk {risk_pct:.3f}% below the "
                            f"{pol.min_risk_pct}% minimum — edge too thin to "
                            f"be worth the execution cost")

    units = (equity * risk_pct / 100.0) / max(risk_per_r, 1e-9)
    return SizeDecision(
        True, risk_pct, units, f, p_used, p_win, b, edge,
        f"quarter-Kelly on a calibration-adjusted p={p_used:.2f} "
        f"(raw {p_win:.2f}, Brier {brier:.3f}) with {b:.2f}:1 net payoff "
        f"-> risk {risk_pct:.2f}% of equity")


def expectancy(p: float, b: float, cost_r: float = 0.0) -> float:
    """Expected R per trade. If this isn't positive, nothing else matters."""
    net_b = max(b - cost_r, 0.0)
    return p * net_b - (1 - p) * (1 + cost_r)


def growth_rate(p: float, b: float, f: float) -> float:
    """Expected log growth per bet — the quantity Kelly maximizes.

    Useful for showing WHY over-betting is fatal: growth is concave in f and
    goes negative well before f reaches 1.
    """
    if f <= 0 or f >= 1:
        return 0.0
    try:
        return p * math.log(1 + f * b) + (1 - p) * math.log(1 - f)
    except ValueError:
        return float("-inf")
