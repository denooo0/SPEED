"""The Lens Foundry: autopilot hypothesis generation, validation, promotion.

The full pipeline, and the order matters:

    GENERATE  →  BEHAVIOR GATE  →  STATISTICAL GATE  →  PARETO ARCHIVE  →  SEAT

  1. GENERATE       random / evolved / LLM-proposed candidates, aimed at the
                    roster's coverage gaps
  2. BEHAVIOR GATE  must be provably distinct from every incumbent. Cheap, runs
                    first, kills most candidates before expensive work.
  3. STATISTICAL    deflated Sharpe + PBO against the TRUE trial count. This is
                    where luck dies.
  4. PARETO         keep a diverse front over (edge, novelty, robustness,
                    coverage) — not a single leaderboard
  5. SEAT           promoted into the Colosseum, on probation, shadow-first

WHY THIS ORDER. The behavior gate is O(archive) and the statistical gate is
O(bars). Running behavior first means the expensive test only ever sees
candidates that would actually add something. It also means novelty is not a
tiebreak applied at the end — it is a precondition.

THE TRIAL COUNTER IS SACRED. `trials_evaluated` counts every candidate that
reached statistical evaluation, and it is what gets deflated against. Resetting
it, or counting only the survivors, converts this whole apparatus into
theatre. It only ever goes up.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..learn.deflated import (ValidationVerdict, minimum_backtest_length,
                              validate_candidate)
from .behavior import BehaviorArchive, BehaviorEmbedding, NoveltyScore, coverage_gaps
from .generate import Generator, build_llm_prompt, parse_llm_proposals
from .grammar import Horizon, LensSpec, Objective, Op, Predicate, RegimeFilter


@dataclass
class LensResult:
    """Everything learned about one candidate."""
    spec: LensSpec
    n_fires: int
    returns: List[float]
    win_rate: float
    mean_r: float
    sharpe: float
    max_dd_r: float
    novelty: Optional[NoveltyScore] = None
    validation: Optional[ValidationVerdict] = None
    rationale: str = ""
    admitted: bool = False
    reject_stage: str = ""

    @property
    def total_r(self) -> float:
        return sum(self.returns)

    def to_dict(self) -> Dict[str, object]:
        return {
            **self.spec.to_dict(),
            "n_fires": self.n_fires, "win_rate": round(self.win_rate, 4),
            "mean_r": round(self.mean_r, 4), "total_r": round(self.total_r, 3),
            "sharpe": round(self.sharpe, 4), "max_dd_r": round(self.max_dd_r, 3),
            "novelty": self.novelty.to_dict() if self.novelty else None,
            "validation": self.validation.to_dict() if self.validation else None,
            "rationale": self.rationale,
            "admitted": self.admitted, "reject_stage": self.reject_stage,
        }


def evaluate_lens(spec: LensSpec, frames: Sequence[Dict[str, float]],
                  forward: Sequence[Dict[str, float]],
                  cost_r: float = 0.10) -> Tuple[List[int], List[float]]:
    """Run a lens over history. Returns (activations, per-trade net R).

    `forward[i]` must contain the realized forward outcome for bar i at each
    horizon, precomputed point-in-time. Costs are subtracted here so every
    downstream statistic is net -- gross R is how a strategy looks profitable
    right up until it is traded.
    """
    acts: List[int] = []
    rets: List[float] = []
    prev: Optional[Dict[str, float]] = None
    key = f"fwd_r_{spec.horizon.value}"
    for i, f in enumerate(frames):
        fired = spec.fires(f, prev)
        prev = f
        if not fired:
            acts.append(0)
            continue
        acts.append(1 if spec.direction_long else -1)
        fwd = forward[i].get(key)
        if fwd is None:
            continue
        r = fwd if spec.direction_long else -fwd
        rets.append(r - cost_r)
    return acts, rets


def _sharpe(rets: Sequence[float]) -> float:
    n = len(rets)
    if n < 2:
        return 0.0
    m = sum(rets) / n
    var = sum((r - m) ** 2 for r in rets) / (n - 1)
    return m / math.sqrt(var) if var > 0 else 0.0


def _max_drawdown(rets: Sequence[float]) -> float:
    peak = cum = 0.0
    dd = 0.0
    for r in rets:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return abs(dd)


@dataclass
class FoundryConfig:
    population: int = 200          # candidates per round
    elite: int = 12                # parents retained for breeding
    min_fires: int = 40            # below this, no statistical claim is possible
    cost_r: float = 0.10
    dsr_threshold: float = 0.95
    pbo_threshold: float = 0.35
    llm_share: float = 0.15        # fraction of each round proposed by the LLM
    evolve_share: float = 0.35
    max_seats: int = 12


class Foundry:
    def __init__(self, cfg: Optional[FoundryConfig] = None, seed: int = 20260803,
                 archive: Optional[BehaviorArchive] = None,
                 llm: Optional[Callable[[str], str]] = None):
        self.cfg = cfg or FoundryConfig()
        self.gen = Generator(seed)
        self.archive = archive or BehaviorArchive()
        self.llm = llm
        self.trials_evaluated = 0        # SACRED. Only ever increases.
        self.rounds = 0
        self.promoted: List[LensResult] = []
        self.pareto: List[LensResult] = []
        self.elites: List[LensSpec] = []
        # Breeding stock, separate from admitted seats.
        #
        # THE BOOTSTRAP BUG THIS FIXES: if elites came only from ADMITTED
        # lenses, then a first round admitting nothing leaves nothing to evolve
        # from — and the foundry random-searches forever, never hill-climbing.
        # Selection pressure (what breeds) and admission (what trades) must be
        # separate concerns: a candidate can be a useful ancestor long before it
        # is a trustworthy signal.
        self.breeding_pool: List[LensResult] = []
        self.history: List[Dict[str, object]] = []
        self.rejections: Dict[str, int] = {}

    def _reject(self, stage: str) -> None:
        self.rejections[stage] = self.rejections.get(stage, 0) + 1

    # ---- one round -------------------------------------------------------

    def run_round(self, frames: Sequence[Dict[str, float]],
                  forward: Sequence[Dict[str, float]],
                  lessons: Optional[Sequence[str]] = None) -> Dict[str, object]:
        self.rounds += 1
        cfg = self.cfg
        n_bars = len(frames)

        candidates: List[Tuple[LensSpec, str]] = []

        # -- aim at uncovered ground rather than firing blind
        gaps = coverage_gaps(self.archive, n_bars)
        gap_objectives = list(Objective)
        n_evolve = int(cfg.population * cfg.evolve_share) if self.elites else 0
        n_llm = int(cfg.population * cfg.llm_share) if self.llm else 0
        n_random = cfg.population - n_evolve - n_llm

        for _ in range(n_random):
            candidates.append((self.gen.targeted(
                self.gen.rng.pick(gap_objectives),
                self.gen.rng.pick(list(RegimeFilter)),
                self.gen.rng.pick(list(Horizon)), self.rounds), ""))

        for _ in range(n_evolve):
            if len(self.elites) >= 2 and self.gen.rng.chance(0.4):
                a = self.gen.rng.pick(self.elites)
                b = self.gen.rng.pick(self.elites)
                candidates.append((self.gen.crossover(a, b), "crossover of elites"))
            elif self.elites:
                candidates.append((self.gen.mutate(
                    self.gen.rng.pick(self.elites)), "mutation of an elite"))

        if self.llm and n_llm:
            try:
                prompt = build_llm_prompt(
                    [r.spec for r in self.promoted[-20:]],
                    [f"bars {a}-{b} ({b-a} bars uncovered)" for a, b in gaps],
                    lessons or [], n_llm)
                candidates.extend(parse_llm_proposals(self.llm(prompt), self.gen,
                                                      self.rounds))
            except Exception:
                pass          # a provider outage must not stall the foundry

        # -- dedupe structurally before doing any work
        seen = set()
        uniq: List[Tuple[LensSpec, str]] = []
        for spec, why in candidates:
            sig = spec.signature()
            if sig in seen:
                self._reject("duplicate_signature")
                continue
            seen.add(sig)
            uniq.append((spec, why))

        results: List[LensResult] = []
        peer_returns: List[List[float]] = []

        for spec, why in uniq:
            acts, rets = evaluate_lens(spec, frames, forward, cfg.cost_r)
            n_fires = sum(1 for a in acts if a != 0)

            if n_fires < cfg.min_fires or len(rets) < cfg.min_fires:
                self._reject("too_few_fires")
                continue

            res = LensResult(
                spec=spec, n_fires=n_fires, returns=rets,
                win_rate=sum(1 for r in rets if r > 0) / len(rets),
                mean_r=sum(rets) / len(rets), sharpe=_sharpe(rets),
                max_dd_r=_max_drawdown(rets), rationale=why)

            # -- GATE 1: behavior. Cheap, and it is a PRECONDITION, not a
            #    tiebreak. A clone never reaches statistical evaluation, so it
            #    never even costs a trial.
            emb = BehaviorEmbedding.build(spec.lens_id, acts)
            nov = self.archive.score(emb)
            res.novelty = nov
            if not nov.admissible:
                res.reject_stage = "behavior"
                self._reject("not_novel")
                results.append(res)
                continue

            # -- GATE 2: statistics. Every candidate that gets here costs a
            #    trial, forever.
            self.trials_evaluated += 1
            peer_returns.append(rets)
            results.append(res)

        # PBO is computed across the round's peers: it asks whether SELECTING
        # the best of this pool generalizes, which is a property of the search,
        # not of any single lens.
        peers = peer_returns if len(peer_returns) >= 4 else None

        admitted: List[LensResult] = []
        for res in results:
            if res.reject_stage:
                continue
            v = validate_candidate(res.returns, self.trials_evaluated,
                                   peer_matrix=peers,
                                   dsr_threshold=cfg.dsr_threshold,
                                   pbo_threshold=cfg.pbo_threshold,
                                   min_returns=cfg.min_fires)
            res.validation = v
            if not v.approved:
                res.reject_stage = "statistics"
                self._reject("failed_deflation")
                continue
            emb = BehaviorEmbedding.build(res.spec.lens_id,
                                          evaluate_lens(res.spec, frames,
                                                        forward, cfg.cost_r)[0])
            final = self.archive.admit(emb)
            if not final.admissible:
                res.reject_stage = "behavior_final"
                self._reject("not_novel_after_peers")
                continue
            res.admitted = True
            admitted.append(res)
            self.promoted.append(res)

        self._update_pareto(admitted)

        # Breeding stock: the best EVALUATED candidates by risk-adjusted edge,
        # whether or not they cleared admission. Admitted lenses get a bonus so
        # proven material is preferred, but a promising-yet-unvalidated
        # candidate is still worth breeding from — that is how the search climbs
        # toward regions where valid lenses actually live.
        scored = [r for r in results if r.n_fires >= cfg.min_fires]
        pool = self.breeding_pool + scored
        pool.sort(key=lambda r: -(r.sharpe + (0.25 if r.admitted else 0.0)))
        seen_sig, trimmed = set(), []
        for r in pool:
            sig = r.spec.signature()
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            trimmed.append(r)
            if len(trimmed) >= cfg.elite * 3:
                break
        self.breeding_pool = trimmed
        self.elites = [r.spec for r in trimmed[:cfg.elite]]

        summary = {
            "round": self.rounds,
            "generated": len(uniq),
            "evaluated": len(results),
            "trials_total": self.trials_evaluated,
            "admitted": len(admitted),
            "roster_size": len(self.archive.members),
            "rejections": dict(self.rejections),
            "diversity": self.archive.diversity_report(),
            "min_backtest_length": minimum_backtest_length(self.trials_evaluated),
            "admitted_lenses": [r.spec.describe() for r in admitted],
        }
        self.history.append(summary)
        return summary

    # ---- Pareto front ----------------------------------------------------

    def _update_pareto(self, new: Sequence[LensResult]) -> None:
        """Multi-objective archive over (edge, novelty, robustness, coverage).

        A single leaderboard would keep four variations of the best idea. The
        Pareto front keeps the lens that is mediocre overall but is the ONLY
        thing covering low-vol Asia — which is exactly the member that makes the
        ensemble worth having.
        """
        pool = self.pareto + list(new)

        def dims(r: LensResult) -> Tuple[float, float, float, float]:
            return (r.sharpe,
                    r.novelty.novelty if r.novelty else 0.0,
                    -r.max_dd_r,
                    float(r.n_fires))

        front: List[LensResult] = []
        for a in pool:
            da = dims(a)
            dominated = any(
                all(db[i] >= da[i] for i in range(4)) and
                any(db[i] > da[i] for i in range(4))
                for b in pool if b is not a for db in [dims(b)])
            if not dominated:
                front.append(a)
        # keep the front bounded, preferring novelty when trimming
        front.sort(key=lambda r: -(r.sharpe + (r.novelty.novelty if r.novelty else 0)))
        self.pareto = front[:self.cfg.max_seats * 3]

    # ---- promotion into the Colosseum ------------------------------------

    def seats(self, n: Optional[int] = None) -> List[LensResult]:
        """The lenses that earned live shelf space: Pareto members, then most
        novel, capped at max_seats."""
        n = n or self.cfg.max_seats
        ranked = sorted(self.pareto,
                        key=lambda r: -(r.sharpe * 0.6 +
                                        (r.novelty.novelty if r.novelty else 0) * 0.4))
        return ranked[:n]

    def report(self) -> Dict[str, object]:
        s = self.seats()
        return {
            "rounds": self.rounds,
            "trials_evaluated": self.trials_evaluated,
            "promoted_total": len(self.promoted),
            "pareto_front": len(self.pareto),
            "active_seats": len(s),
            "rejections": dict(self.rejections),
            "diversity": self.archive.diversity_report(),
            "honesty_note": (
                f"Every statistic above is deflated against "
                f"{self.trials_evaluated} trials. That counter never resets. "
                f"A lens that looks good but has not cleared deflation is not "
                f"on this list, no matter how good its equity curve looks."),
            "seats": [r.to_dict() for r in s],
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.report(), indent=2, default=str))
        return p
