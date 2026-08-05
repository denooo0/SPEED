"""Behavior space: lens embeddings and the novelty gate.

THIS IS THE ANSWER TO "how do we get diversity without tripping consensus
collapse." The insight that makes it work:

    Stop treating orthogonality as a symptom you MONITOR.
    Make it an ADMISSION REQUIREMENT.

v1 had a `consensus_collapse` detector that watched inter-seat correlation and
complained when the seats converged. That is a smoke alarm. It tells you the
house is already burning. A candidate lens should not be able to take a seat in
the first place unless it is provably different from every incumbent.

WHAT "DIFFERENT" MEANS, PRECISELY. Not different code. Not different features.
Different BEHAVIOR — the pattern of *when it fires and which way*. Two lenses
built from completely different indicators that happen to fire at the same
moments on the same side are the same lens wearing a costume. Two lenses sharing
every input that fire at disjoint times are genuinely diverse.

So each lens gets an EMBEDDING: its activation vector over a reference window,
in {-1, 0, +1} per bar. That vector is the lens's identity. Novelty is distance
in that space. This is Lehman & Stanley's novelty search, borrowed from
evolutionary robotics, where it solves exactly this problem — search that
collapses onto one local optimum.

Three metrics, because they catch different failures:

  COSINE on activations     -- do they call the same direction at the same time?
  JACCARD on firing times   -- do they even fire at the same moments?
  CONDITIONAL AGREEMENT     -- when BOTH fire, how often do they agree?

A clone scores high on all three. A genuinely novel lens must be far on at
least one, and the archive enforces it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class BehaviorEmbedding:
    """A lens's identity: what it DID, not what it is made of."""
    lens_id: str
    activations: Tuple[int, ...]     # -1 short, 0 flat, +1 long, per bar
    fire_rate: float
    long_share: float
    _fired_idx: frozenset = field(default=frozenset(), repr=False)

    @staticmethod
    def build(lens_id: str, activations: Sequence[int]) -> "BehaviorEmbedding":
        acts = tuple(int(a) for a in activations)
        fired = frozenset(i for i, a in enumerate(acts) if a != 0)
        n = len(acts) or 1
        longs = sum(1 for a in acts if a > 0)
        return BehaviorEmbedding(
            lens_id=lens_id, activations=acts,
            fire_rate=len(fired) / n,
            long_share=longs / max(len(fired), 1),
            _fired_idx=fired)

    @property
    def n_fires(self) -> int:
        return len(self._fired_idx)


def cosine(a: BehaviorEmbedding, b: BehaviorEmbedding) -> float:
    """Directional agreement over the whole window. 1 = identical behavior."""
    n = min(len(a.activations), len(b.activations))
    if n == 0:
        return 0.0
    dot = sum(a.activations[i] * b.activations[i] for i in range(n))
    na = math.sqrt(sum(a.activations[i] ** 2 for i in range(n)))
    nb = math.sqrt(sum(b.activations[i] ** 2 for i in range(n)))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def jaccard(a: BehaviorEmbedding, b: BehaviorEmbedding) -> float:
    """Timing overlap, ignoring direction. Two lenses that never fire together
    are diverse even if they would agree when they did."""
    if not a._fired_idx and not b._fired_idx:
        return 0.0
    inter = len(a._fired_idx & b._fired_idx)
    union = len(a._fired_idx | b._fired_idx)
    return inter / union if union else 0.0


def conditional_agreement(a: BehaviorEmbedding, b: BehaviorEmbedding) -> float:
    """When BOTH fire, how often do they agree? Catches the subtle clone: a lens
    that fires rarely but always echoes an incumbent when it does."""
    both = a._fired_idx & b._fired_idx
    if not both:
        return 0.0
    agree = sum(1 for i in both
                if a.activations[i] == b.activations[i])
    return agree / len(both)


@dataclass(frozen=True)
class NoveltyScore:
    novelty: float                 # 0 = clone, 1 = maximally novel
    nearest_id: str
    max_cosine: float
    max_jaccard: float
    max_cond_agreement: float
    admissible: bool
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {"novelty": round(self.novelty, 4),
                "nearest": self.nearest_id,
                "max_cosine": round(self.max_cosine, 4),
                "max_jaccard": round(self.max_jaccard, 4),
                "max_conditional_agreement": round(self.max_cond_agreement, 4),
                "admissible": self.admissible, "reason": self.reason}


class BehaviorArchive:
    """The gate. Nothing enters the Colosseum without clearing it.

    Thresholds are deliberately strict. The cost of admitting a clone is not
    just wasted compute -- it is FALSE CONFLUENCE. The arbiter treats two seats
    agreeing as independent corroboration and raises conviction accordingly. If
    those two seats are secretly the same lens, the engine systematically
    over-bets on exactly the setups where it is least diversified. That is how
    an ensemble kills you.
    """

    def __init__(self, *, max_cosine: float = 0.70,
                 max_jaccard: float = 0.60,
                 max_cond_agreement: float = 0.85,
                 min_fire_rate: float = 0.0005,
                 max_fire_rate: float = 0.25,
                 capacity: int = 300):
        self.max_cosine = max_cosine
        self.max_jaccard = max_jaccard
        self.max_cond_agreement = max_cond_agreement
        self.min_fire_rate = min_fire_rate
        self.max_fire_rate = max_fire_rate
        self.capacity = capacity
        self.members: Dict[str, BehaviorEmbedding] = {}
        self.rejections: Dict[str, int] = {}

    def _note(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    def score(self, emb: BehaviorEmbedding) -> NoveltyScore:
        # -- activity sanity first: a lens that never fires cannot be evaluated,
        #    and one that fires constantly is a regime indicator, not a signal.
        if emb.fire_rate < self.min_fire_rate:
            self._note("too_quiet")
            return NoveltyScore(0.0, "-", 0, 0, 0, False,
                                f"fires on {emb.fire_rate:.4%} of bars — too "
                                f"rare to ever accumulate evidence")
        if emb.fire_rate > self.max_fire_rate:
            self._note("too_loud")
            return NoveltyScore(0.0, "-", 0, 0, 0, False,
                                f"fires on {emb.fire_rate:.1%} of bars — that is "
                                f"a market-state descriptor, not a signal")

        if not self.members:
            return NoveltyScore(1.0, "-", 0.0, 0.0, 0.0, True,
                                "first member of the archive")

        worst_cos = worst_jac = worst_agr = 0.0
        nearest = "-"
        best_dist = 1.0
        for mid, m in self.members.items():
            c = abs(cosine(emb, m))
            j = jaccard(emb, m)
            a = conditional_agreement(emb, m)
            # Composite distance: a candidate must be far on the WORST axis.
            d = 1.0 - max(c, j * a)
            if d < best_dist:
                best_dist, nearest = d, mid
            worst_cos = max(worst_cos, c)
            worst_jac = max(worst_jac, j)
            worst_agr = max(worst_agr, a)

        if worst_cos > self.max_cosine:
            self._note("cosine_clone")
            return NoveltyScore(best_dist, nearest, worst_cos, worst_jac,
                                worst_agr, False,
                                f"directional behavior {worst_cos:.2f} correlated "
                                f"with {nearest} — this is a clone. Admitting it "
                                f"would create false confluence.")
        if worst_jac > self.max_jaccard and worst_agr > self.max_cond_agreement:
            self._note("timing_clone")
            return NoveltyScore(best_dist, nearest, worst_cos, worst_jac,
                                worst_agr, False,
                                f"fires at the same times as {nearest} "
                                f"(jaccard {worst_jac:.2f}) and agrees "
                                f"{worst_agr:.0%} of the time when it does")
        return NoveltyScore(best_dist, nearest, worst_cos, worst_jac, worst_agr,
                            True,
                            f"behaviorally distinct (nearest {nearest} at "
                            f"distance {best_dist:.2f})")

    def admit(self, emb: BehaviorEmbedding) -> NoveltyScore:
        s = self.score(emb)
        if s.admissible:
            self.members[emb.lens_id] = emb
            if len(self.members) > self.capacity:
                self._evict()
        return s

    def _evict(self) -> None:
        """Evict the LEAST NOVEL member, never the oldest. Age is not a crime;
        redundancy is. Keeping the archive maximally spread is the whole point."""
        if len(self.members) <= 2:
            return
        worst_id, worst_dist = None, 2.0
        ids = list(self.members)
        for i, mid in enumerate(ids):
            m = self.members[mid]
            d = min((1.0 - abs(cosine(m, self.members[o]))
                     for o in ids if o != mid), default=1.0)
            if d < worst_dist:
                worst_dist, worst_id = d, mid
        if worst_id:
            self.members.pop(worst_id, None)

    def remove(self, lens_id: str) -> None:
        self.members.pop(lens_id, None)

    def diversity_report(self) -> Dict[str, object]:
        ids = list(self.members)
        if len(ids) < 2:
            return {"members": len(ids), "mean_pairwise_distance": 1.0,
                    "min_pairwise_distance": 1.0, "rejections": dict(self.rejections)}
        dists = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                dists.append(1.0 - abs(cosine(self.members[ids[i]],
                                              self.members[ids[j]])))
        return {"members": len(ids),
                "mean_pairwise_distance": round(sum(dists) / len(dists), 4),
                "min_pairwise_distance": round(min(dists), 4),
                "rejections": dict(self.rejections),
                "note": ("min_pairwise_distance is the number to watch. If it "
                         "drifts toward 0 the roster is converging even though "
                         "every member passed admission individually.")}


def coverage_gaps(archive: BehaviorArchive, n_bars: int,
                  top_k: int = 5) -> List[Tuple[int, int]]:
    """Which parts of history does NOBODY cover?

    This turns diversity from a constraint into a SEARCH DIRECTION. Instead of
    generating candidates at random and hoping they are novel, aim the generator
    at the windows where the current roster is silent. That is where new edge
    can actually live -- everywhere else is already spoken for.
    """
    if not archive.members:
        return [(0, n_bars)]
    covered = [0] * n_bars
    for m in archive.members.values():
        for i in m._fired_idx:
            if i < n_bars:
                covered[i] += 1
    gaps: List[Tuple[int, int]] = []
    start = None
    for i, c in enumerate(covered):
        if c == 0 and start is None:
            start = i
        elif c > 0 and start is not None:
            gaps.append((start, i))
            start = None
    if start is not None:
        gaps.append((start, n_bars))
    gaps.sort(key=lambda g: -(g[1] - g[0]))
    return gaps[:top_k]
