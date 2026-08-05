"""Roster evolution: challengers, shadow validation, and FDR-controlled promotion.

This is the part of the Colosseum that keeps it from becoming a museum. Seats
that stop earning get relegated; mutated variants attack the incumbents; the
winners take shelf space.

The danger it must survive is p-hacking. Run fifty challenger lenses and one
will look brilliant purely by luck -- promote it and you have installed noise as
your champion. Two defences, both mandatory:

  1. SHADOW VALIDATION -- a challenger runs on live data producing paper signals
     only. It must beat the incumbent OUT OF SAMPLE, on the same tape, over a
     minimum number of graded predictions.
  2. BENJAMINI-HOCHBERG FDR CONTROL -- when several challengers are evaluated at
     once, the significance bar rises accordingly. Testing more ideas must not
     make it easier to promote a lucky one.

A challenger that clears both is promoted; the demoted champion is archived, not
deleted, so a bad promotion can always be reverted (Guardian rung 4).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .elo import Colosseum, SeatRating


@dataclass
class Mutation:
    """A perturbation of a lens's parameters. Deterministic given a seed, so a
    promotion decision can be reproduced exactly during an audit."""
    param: str
    from_value: float
    to_value: float

    def describe(self) -> str:
        return f"{self.param}: {self.from_value:.3g} -> {self.to_value:.3g}"


@dataclass
class Challenger:
    challenger_id: str
    parent_seat: str
    lens: str
    mutations: List[Mutation]
    created_n: int                       # parent's sample count at creation
    shadow_rewards: List[float] = field(default_factory=list)
    shadow_wins: int = 0
    shadow_n: int = 0
    status: str = "shadow"               # shadow | promoted | rejected | expired
    p_value: float = 1.0
    verdict_note: str = ""

    @property
    def mean_reward(self) -> float:
        return (sum(self.shadow_rewards) / len(self.shadow_rewards)
                if self.shadow_rewards else 0.0)

    @property
    def win_rate(self) -> float:
        return self.shadow_wins / self.shadow_n if self.shadow_n else 0.0


def welch_t_test(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float]:
    """Welch's t-test: does NOT assume equal variance.

    That matters here -- a challenger tuned for wider stops has structurally
    different reward variance than its parent, and Student's t would over-reject.
    Returns (t_statistic, approximate two-sided p-value).
    """
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 0.0, 1.0
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se2 = va / na + vb / nb
    if se2 <= 0:
        return 0.0, 1.0
    t = (ma - mb) / math.sqrt(se2)
    df = se2 ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    return t, _t_sf_two_sided(abs(t), df)


def _t_sf_two_sided(t: float, df: float) -> float:
    """Two-sided p from the t distribution via the incomplete beta function.
    Implemented directly to keep the engine stdlib-only."""
    if df <= 0:
        return 1.0
    x = df / (df + t * t)
    return max(0.0, min(1.0, _betainc(df / 2.0, 0.5, x)))


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1 - x) / b


def _betacf(a: float, b: float, x: float, itmax: int = 200,
            eps: float = 3e-12) -> float:
    """Continued-fraction expansion (Lentz's method)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < 1e-30:
            d = 1e-30
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < 1e-30:
            d = 1e-30
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def benjamini_hochberg(p_values: Sequence[float], fdr: float = 0.10
                       ) -> Tuple[List[bool], float]:
    """BH step-up procedure. Returns (reject_flags, critical_threshold).

    Controls the expected proportion of FALSE promotions among all promotions.
    Testing ten challengers does not get you ten chances at the same bar -- the
    bar itself tightens. This is the difference between evolution and gambling.
    """
    n = len(p_values)
    if n == 0:
        return [], 0.0
    order = sorted(range(n), key=lambda i: p_values[i])
    crit = 0.0
    k_max = -1
    for rank, i in enumerate(order, start=1):
        thresh = fdr * rank / n
        if p_values[i] <= thresh:
            k_max = rank
            crit = thresh
    flags = [False] * n
    if k_max > 0:
        for rank, i in enumerate(order, start=1):
            if rank <= k_max:
                flags[i] = True
    return flags, crit


class ChallengerRegistry:
    """Spawns, shadows, and adjudicates challengers."""

    def __init__(self, colosseum: Colosseum, *, min_shadow_n: int = 40,
                 fdr: float = 0.10, min_edge: float = 0.15,
                 max_active: int = 4, expire_after_n: int = 400,
                 seed: int = 7777):
        self.col = colosseum
        self.min_shadow_n = min_shadow_n
        self.fdr = fdr
        self.min_edge = min_edge
        self.max_active = max_active
        self.expire_after_n = expire_after_n
        self.challengers: Dict[str, Challenger] = {}
        self.promotions: List[Dict[str, object]] = []
        self.archive: List[Dict[str, object]] = []
        self._rng = seed & 0xFFFFFFFF or 1
        self._seq = 0

    def _rand(self) -> float:
        x = self._rng
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self._rng = x & 0xFFFFFFFF
        return self._rng / 0xFFFFFFFF

    # ---- spawning --------------------------------------------------------

    def spawn(self, parent_seat: str, base_params: Dict[str, float],
              n_mutations: int = 2) -> Optional[Challenger]:
        """Mutate a parent's parameters. Mutations are multiplicative and
        bounded -- a challenger should be a variation on a working idea, not a
        random walk into nonsense."""
        active = [c for c in self.challengers.values() if c.status == "shadow"]
        if len(active) >= self.max_active:
            return None
        parent = self.col.seats.get(parent_seat)
        if parent is None:
            return None

        keys = list(base_params.keys())
        if not keys:
            return None
        muts: List[Mutation] = []
        for _ in range(min(n_mutations, len(keys))):
            k = keys[int(self._rand() * len(keys)) % len(keys)]
            if any(m.param == k for m in muts):
                continue
            old = float(base_params[k])
            factor = 0.7 + self._rand() * 0.6        # 0.7x .. 1.3x
            muts.append(Mutation(k, old, round(old * factor, 4)))
        if not muts:
            return None

        self._seq += 1
        cid = f"CHL-{parent_seat}-{self._seq:03d}"
        ch = Challenger(cid, parent_seat, f"{parent.lens} (variant {self._seq})",
                        muts, parent.n)
        self.challengers[cid] = ch
        return ch

    # ---- shadow accumulation --------------------------------------------

    def record_shadow(self, challenger_id: str, reward: float, won: bool) -> None:
        ch = self.challengers.get(challenger_id)
        if ch is None or ch.status != "shadow":
            return
        ch.shadow_rewards.append(reward)
        ch.shadow_n += 1
        ch.shadow_wins += 1 if won else 0

    # ---- adjudication ----------------------------------------------------

    def evaluate(self, parent_rewards: Dict[str, List[float]]
                 ) -> List[Dict[str, object]]:
        """Adjudicate every ready challenger together, under FDR control.

        Evaluating jointly is deliberate: it is the only way the multiple-testing
        correction can see how many bets were actually placed.
        """
        ready = [c for c in self.challengers.values()
                 if c.status == "shadow" and c.shadow_n >= self.min_shadow_n]
        if not ready:
            self._expire()
            return []

        ps, edges = [], []
        for ch in ready:
            base = parent_rewards.get(ch.parent_seat, [])
            t, p = welch_t_test(ch.shadow_rewards, base)
            # One-sided intent: only BEATING the parent counts. Halve the
            # two-sided p when the challenger is ahead, else treat as no evidence.
            edge = ch.mean_reward - (sum(base) / len(base) if base else 0.0)
            ch.p_value = (p / 2.0) if edge > 0 else 1.0
            ps.append(ch.p_value)
            edges.append(edge)

        flags, crit = benjamini_hochberg(ps, self.fdr)
        results: List[Dict[str, object]] = []
        for ch, ok, edge in zip(ready, flags, edges):
            passes_edge = edge >= self.min_edge
            if ok and passes_edge:
                ch.status = "promoted"
                ch.verdict_note = (
                    f"beat parent by {edge:+.3f} mean reward over {ch.shadow_n} "
                    f"shadow calls (p={ch.p_value:.4f} <= BH threshold "
                    f"{crit:.4f} at FDR {self.fdr})")
                self._promote(ch, edge)
            else:
                ch.status = "rejected"
                if not passes_edge:
                    ch.verdict_note = (f"edge {edge:+.3f} below required "
                                       f"{self.min_edge} -- statistically "
                                       f"visible but not economically useful")
                else:
                    ch.verdict_note = (f"p={ch.p_value:.4f} did not clear the "
                                       f"BH threshold {crit:.4f}; with "
                                       f"{len(ready)} challengers tested, this "
                                       f"is most likely luck")
            results.append({"challenger_id": ch.challenger_id,
                            "parent": ch.parent_seat, "status": ch.status,
                            "edge": round(edge, 4), "p_value": round(ch.p_value, 5),
                            "bh_threshold": round(crit, 5),
                            "shadow_n": ch.shadow_n, "note": ch.verdict_note})
        self._expire()
        return results

    def _promote(self, ch: Challenger, edge: float) -> None:
        """Champion/challenger swap. The incumbent is ARCHIVED with full state so
        Guardian rung 4 (rollback_roster) can restore it if the promotion sours."""
        parent = self.col.seats.get(ch.parent_seat)
        if parent is None:
            return
        self.archive.append({"seat_id": parent.seat_id, "lens": parent.lens,
                             "elo": parent.elo, "n": parent.n,
                             "snapshot": parent.to_dict(),
                             "replaced_by": ch.challenger_id})
        parent.lens = ch.lens
        # Start on probation at a modest discount, never at the parent's peak:
        # a shadow record is evidence, not a guarantee, and probation makes the
        # promotion cheap to reverse.
        parent.elo = max(1450.0, parent.elo * 0.97)
        parent.status = "probation"
        parent.benched_until_n = parent.n + 25
        self.promotions.append({
            "challenger_id": ch.challenger_id, "seat_id": parent.seat_id,
            "mutations": [m.describe() for m in ch.mutations],
            "edge": round(edge, 4), "p_value": round(ch.p_value, 5),
            "note": ch.verdict_note})

    def _expire(self) -> None:
        for ch in self.challengers.values():
            if ch.status != "shadow":
                continue
            parent = self.col.seats.get(ch.parent_seat)
            if parent and parent.n - ch.created_n > self.expire_after_n:
                ch.status = "expired"
                ch.verdict_note = ("never accumulated enough shadow evidence "
                                   "before its window closed")

    def report(self) -> Dict[str, object]:
        by_status: Dict[str, int] = {}
        for c in self.challengers.values():
            by_status[c.status] = by_status.get(c.status, 0) + 1
        return {"total": len(self.challengers), "by_status": by_status,
                "promotions": self.promotions[-10:],
                "archived_champions": len(self.archive)}
