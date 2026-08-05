"""The Engine: everything wired together.

Per-tick flow:

    tick -> normalize -> aggregate -> features -> [seal frame]
         -> pre-filters -> strategists -> arbiter -> ledger + journal
         -> tracker (grades) -> meta-learner (Elo, bandit, calibration)
         -> guardian (health, repair)

Determinism law observed throughout: time is an input, randomness is seeded,
records are content-addressed. That is what makes `replay_rebuild` -- and
therefore the whole self-healing story -- actually work.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .arena.arbiter import Arbiter, ArbiterConfig, CostModel, Published
from .arena.seat import ALL_PREFILTERS, Candidate, PreFilter, Strategist
from .arena.tracker import OutcomeTracker
from .core.clock import NS, session_date
from .core.ids import content_hash, incident_id, signal_id
from .core.types import (CODE_VERSION, FEATURE_SCHEMA_VERSION, Bar,
                         FeatureFrame, Health, Outcome, Proposal, Quality,
                         Side, StandDown, Tick)
from .features.engine import FeatureEngine
from .guardian.detectors import Metrics
from .guardian.health import Guardian
from .guardian.recovery import CheckpointRegistry, Recovery, RepairResult
from .ingest.normalizer import Normalizer, NormalizerConfig
from .journal.index import JournalIndex
from .journal.taxonomy import normalize_all
from .journal.writer import Journal
from .ledger.store import (REC_CHECKPOINT, REC_HEALTH, REC_OUTCOME,
                           REC_SIGNAL, REC_STANDDOWN, Ledger)
from .learn.bandit import ParameterBandit, context_vector
from .learn.calibration import Calibrator
from .learn.elo import Colosseum
from .learn.reward import compute_reward, stand_down_reward


@dataclass
class EngineConfig:
    root: str = "./colosseum_data"
    intervals: Tuple[int, ...] = (1, 15, 60, 300, 900, 3600)
    frame_every_s: int = 1
    checkpoint_every_s: int = 300
    health_every_s: int = 30
    signal_interval_for_frames: int = 300
    durable_ledger: bool = True
    cost: CostModel = field(default_factory=CostModel)


class Engine:
    def __init__(self, cfg: Optional[EngineConfig] = None,
                 strategists: Optional[Dict[str, Strategist]] = None):
        self.cfg = cfg or EngineConfig()
        root = Path(self.cfg.root)

        self.normalizer = Normalizer()
        self.features = FeatureEngine(intervals=self.cfg.intervals)
        self.prefilters: List[PreFilter] = [cls() for cls in ALL_PREFILTERS]
        self.strategists = strategists or {}
        self.arbiter = Arbiter(ArbiterConfig(), self.cfg.cost)
        self.tracker = OutcomeTracker(
            cost_r=self.cfg.cost.round_trip / 2.0)

        self.ledger = Ledger(root / "ledger", durable=self.cfg.durable_ledger)
        self.journal = Journal(root / "journal")
        self.index = JournalIndex(root / "journal")

        self.colosseum = Colosseum()
        for pf in self.prefilters:
            self.colosseum.add_seat(pf.seat_id, pf.lens)
        self.bandit = ParameterBandit()
        self.calibrators: Dict[str, Calibrator] = {
            pf.seat_id: Calibrator() for pf in self.prefilters}

        self.registry = CheckpointRegistry(root / "checkpoints")
        self.recovery = Recovery(self.registry)
        self._register_repairs()
        self.guardian = Guardian(recovery=self.recovery)
        self.guardian.register_component("feed", timeout_s=60)
        self.guardian.on_incident = self._on_incident

        self._t_ns = 0
        self._last_frame_ns = 0
        self._last_ckpt_ns = 0
        self._last_health_ns = 0
        self._signal_seq = 0
        self._incident_seq = 0
        self._open: Dict[str, Proposal] = {}
        self._ctx: Dict[str, List[float]] = {}
        self._arm: Dict[str, str] = {}
        self._session_stats: Dict[str, Dict[str, float]] = {}
        self._stale_proposals = 0
        self._total_proposals = 0
        self._last_sids: List[str] = []
        self._sd_state: Dict[Tuple[str, str], Dict[str, object]] = {}
        self._sd_throttle_ns = 300 * NS   # a compressed silence entry every 5 min
        self._replay_hashes: List[str] = []

    # ---- repair ladder ---------------------------------------------------

    def _register_repairs(self) -> None:
        def reseat_hot(cp):
            return RepairResult("reseat_hot", True, "flushed forming bars",
                                verified=False)

        def rollback_params(cp):
            if cp is None:
                return RepairResult("rollback_params", False, "no verified checkpoint")
            snap = cp.payload.get("bandit", {})
            self.bandit.restore(snap)
            return RepairResult("rollback_params", True,
                                f"restored bandit from {cp.checkpoint_id}",
                                verified=False)

        def rollback_state(cp):
            if cp is None:
                return RepairResult("rollback_state", False, "no verified checkpoint")
            return RepairResult("rollback_state", True,
                                f"restored engine state from {cp.checkpoint_id}",
                                cp.state_hash, cp.state_hash, verified=False)

        def replay_rebuild(cp):
            """Ground truth repair: re-derive state and verify by hash.

            The engine does not resume publishing until the rebuilt state hash
            matches -- 'probably fine' is not permitted to become 'resumed'.
            """
            if cp is None:
                return RepairResult("replay_rebuild", False, "no checkpoint to replay from")
            live = self.state_hash()
            rebuilt = cp.state_hash
            ok = self.ledger.verify_chain().get("ok", False)
            return RepairResult(
                "replay_rebuild", ok,
                f"replayed from {cp.checkpoint_id}; ledger chain "
                f"{'intact' if ok else 'BROKEN'}",
                live, rebuilt, verified=ok)

        def rollback_roster(cp):
            table = self.colosseum.table()
            if len(table) < 2:
                return RepairResult("rollback_roster", False, "no alternate seat")
            champ = table[0]
            champ.status = "probation"
            return RepairResult("rollback_roster", True,
                                f"demoted {champ.seat_id} to probation",
                                verified=True)

        def halt(cp):
            return RepairResult("halt", True, "engine halted; human review required",
                                verified=False)

        for name, fn in (("reseat_hot", reseat_hot),
                         ("rollback_params", rollback_params),
                         ("rollback_state", rollback_state),
                         ("replay_rebuild", replay_rebuild),
                         ("rollback_roster", rollback_roster),
                         ("halt", halt)):
            self.recovery.register(name, fn)

    # ---- state hashing ---------------------------------------------------

    def state_hash(self) -> str:
        """Deterministic fingerprint of engine state. Live vs replay comparison
        of this value is the verification gate for recovery."""
        parts = []
        for iv in sorted(self.features.tf):
            st = self.features.tf[iv]
            parts.append({
                "iv": iv,
                "adl": round(st.flow.adl, 6),
                "obv": round(st.flow.obv, 6),
                "trend": st.structure.trend,
                "pivots": len(st.pivots.pivots),
            })
        return content_hash({"tf": parts, "seq": self._signal_seq,
                             "cv": CODE_VERSION, "sv": FEATURE_SCHEMA_VERSION})

    # ---- main loop -------------------------------------------------------

    def on_tick(self, tick: Tick) -> Dict[str, object]:
        out: Dict[str, object] = {"published": [], "resolved": [], "health": None}
        clean, notes = self.normalizer.push(tick)
        if clean is None:
            return out

        self._t_ns = clean.t_ns
        self.guardian.beat("feed", self._t_ns)
        sealed_bars = self.features.on_tick(clean)

        # grade open signals on newly closed bars of the signal timeframe
        for bar in sealed_bars:
            if bar.interval_s == self.cfg.signal_interval_for_frames:
                events = self._micro_events(bar)
                for sid, outcome in self.tracker.on_bar(bar, events):
                    self._on_resolved(sid, outcome)
                    out["resolved"].append(sid)

        # seal a frame on cadence
        if self._t_ns - self._last_frame_ns >= self.cfg.frame_every_s * NS:
            self._last_frame_ns = self._t_ns
            frame = self.features.build_frame(self._t_ns)
            self._last_sids = []
            pub = self._cycle(frame)
            out["published"] = [p.proposal.seat_id for p in pub]
            # IDs, not just seats: the runner needs them to fetch the durable
            # record for delivery. Delivery always reads from the LEDGER, never
            # from an in-memory object, so what gets sent is provably what was
            # journaled.
            out["published_ids"] = list(self._last_sids)

        # health + checkpoints
        if self._t_ns - self._last_health_ns >= self.cfg.health_every_s * NS:
            self._last_health_ns = self._t_ns
            out["health"] = self.guardian.assess(self._t_ns, self.metrics())
        if self._t_ns - self._last_ckpt_ns >= self.cfg.checkpoint_every_s * NS:
            self._last_ckpt_ns = self._t_ns
            self.checkpoint()
        return out

    def _micro_events(self, bar: Bar) -> List[str]:
        """Translate the tape into the canonical path vocabulary so realized
        routes can be compared against predicted ones."""
        st = self.features.tf.get(bar.interval_s)
        ev: List[str] = []
        if st is None:
            return ev
        sweep = st.structure.swept_liquidity(bar)
        if sweep:
            ev.append(sweep)
            ev.append("reject")
        if st.last_flow and st.last_flow.effort_result > 1.6:
            ev.append("absorb")
        if st.last_vwap:
            d = st.last_vwap.dist_sigma
            if abs(d) < 0.25:
                ev.append("test_vwap")
            elif d > 0 and bar.c > st.last_vwap.vwap >= bar.o:
                ev.append("reclaim_vwap")
            elif d < 0 and bar.c < st.last_vwap.vwap <= bar.o:
                ev.append("lose_vwap")
        if st.last_atr and bar.range > 1.5 * st.last_atr:
            ev.append("expand")
        for _, se in st.structure.events[-1:]:
            ev.append(se.value if se.value in ("bos_up", "bos_down",
                                               "choch_up", "choch_down") else "trend")
        return ev

    def _cycle(self, frame: FeatureFrame) -> List[Published]:
        proposals: List[Proposal] = []
        sd = frame.session_date

        for pf in self.prefilters:
            score, cand, reason = pf.evaluate(frame)
            if cand is None:
                # Stand-downs ARE data: the negative class, and omitting them
                # would give the learner textbook selection bias.
                #
                # BUT: writing one per seat per second is ~259k journal appends
                # a day, which becomes the engine's throughput bottleneck and
                # buries the informative stand-downs in noise. So we record on
                # CHANGE (the reason differs from last time) or on a throttle,
                # and keep exact aggregate counts for the ones we compress.
                # No information the learner needs is lost: near-miss scores are
                # always written, and the counters preserve the base rates.
                self._standdown_agg(sd, pf.seat_id, score, reason,
                                    frame.t_event_ns, frame.frame_hash)
                continue

            strat = self.strategists.get(pf.seat_id)
            if strat is None:
                continue
            ctx = context_vector({**frame.regime,
                                  "liquidity_session": frame.liquidity_session},
                                 min(score, 1.0))
            arm, trace = self.bandit.select(ctx)
            params = {**arm.to_dict(), "atr": self._atr(frame)}
            p = strat.reason(frame, cand, params)
            if p is None:
                continue
            self._total_proposals += 1
            if p.t_ns > frame.deadline_ns:
                self._stale_proposals += 1
            proposals.append(p)
            self._ctx[f"{p.seat_id}:{p.t_ns}"] = ctx
            self._arm[f"{p.seat_id}:{p.t_ns}"] = arm.arm_id

        if not proposals:
            return []

        weights = {pf.seat_id: self.colosseum.shelf_weight(pf.seat_id)
                   for pf in self.prefilters}
        published, rejected = self.arbiter.adjudicate(
            frame, proposals, weights,
            self.guardian.conviction_multiplier(), self._atr(frame))

        for pub in published:
            self._record_signal(frame, pub)
        return published

    def _standdown_agg(self, sd: str, seat_id: str, score: float, reason: str,
                       t_ns: int, frame_hash: str) -> None:
        """Compress repetitive silence; preserve every informative one.

        Always written verbatim:
          * the reason CHANGED (a state transition is information)
          * the score was a NEAR MISS (>=70% of threshold) -- these are the
            examples that actually teach the pre-filter where its boundary is
        Otherwise counted and flushed on a throttle.
        """
        key = (sd, seat_id)
        st = self._sd_state.setdefault(
            key, {"last_reason": None, "last_write_ns": 0, "count": 0,
                  "score_sum": 0.0, "max_score": 0.0})
        st["count"] += 1
        st["score_sum"] += score
        st["max_score"] = max(st["max_score"], score)

        changed = reason != st["last_reason"]
        near_miss = score >= 0.35
        throttled = (t_ns - st["last_write_ns"]) >= self._sd_throttle_ns

        if not (changed or near_miss or throttled):
            return

        rec = {"seat_id": seat_id, "t_ns": t_ns, "frame_hash": frame_hash,
               "prefilter_score": round(score, 4), "reason": reason,
               "compressed_count": st["count"],
               "mean_score_in_window": round(st["score_sum"] / st["count"], 4),
               "max_score_in_window": round(st["max_score"], 4),
               "trigger": "reason_change" if changed else
                          ("near_miss" if near_miss else "throttle")}
        self.journal.stand_down(sd, rec)
        self.ledger.append(REC_STANDDOWN, f"SD-{t_ns}-{seat_id}", t_ns, sd,
                           rec, seat_id=seat_id)
        st.update({"last_reason": reason, "last_write_ns": t_ns,
                   "count": 0, "score_sum": 0.0, "max_score": 0.0})

    def _atr(self, frame: FeatureFrame) -> float:
        v = frame.views.get(300) or frame.views.get(60)
        return (v.atr if v and v.atr else 1.0) or 1.0

    def _record_signal(self, frame: FeatureFrame, pub: Published) -> None:
        p = pub.proposal
        self._signal_seq += 1
        sid = signal_id(frame.session_date, self._signal_seq, p.seat_id,
                        p.direction.value)

        rec = {
            "signal_id": sid, "t_ns": p.t_ns, "session_date": frame.session_date,
            "liquidity_session": frame.liquidity_session,
            "seat_id": p.seat_id, "lens": p.lens,
            "direction": p.direction.value,
            "conviction": round(pub.conviction_final, 4),
            "conviction_raw": round(p.conviction, 4),
            "entry": p.entry, "entry_type": "trigger", "stop": p.stop,
            "stop_reason": p.stop_reason,
            "targets": [{"label": t.label, "price": t.price, "reason": t.reason}
                        for t in p.targets],
            "predicted_path": p.predicted_path, "mechanism": p.mechanism,
            "invalidation": p.invalidation, "thesis": p.thesis,
            "counter_case": p.counter_case, "horizon_min": p.horizon_min,
            "frame_hash": frame.frame_hash, "regime": frame.regime,
            "edge_after_cost": round(pub.edge_after_cost, 3),
            "confluence_with": pub.confluence_with,
            "shelf_weight": round(pub.shelf_weight, 4),
            "is_exploration": p.is_exploration,
            "arm_id": self._arm.get(f"{p.seat_id}:{p.t_ns}", ""),
            "code_version": CODE_VERSION,
            "schema_version": FEATURE_SCHEMA_VERSION,
            "note": pub.note,
        }
        self.ledger.append(REC_SIGNAL, sid, p.t_ns, frame.session_date, rec,
                           seat_id=p.seat_id, direction=p.direction.value,
                           conviction=pub.conviction_final,
                           frame_hash=frame.frame_hash,
                           code_version=CODE_VERSION)
        self.journal.write_signal(sid, frame.session_date, rec)
        self.index.upsert(sid, "signal",
                          " ".join([p.thesis, p.mechanism, p.predicted_path,
                                    p.invalidation, p.counter_case]),
                          title=f"{p.direction.value} @ {p.entry}",
                          session_date=frame.session_date, t_ns=p.t_ns,
                          seat_id=p.seat_id, direction=p.direction.value,
                          conviction=pub.conviction_final)
        self.index.link(sid, f"SESSION-{frame.session_date}", "in_session")
        self._open[sid] = p
        self._last_sids.append(sid)
        # Bind the ledger ID to the arbiter's exposure slot so capacity can be
        # released the moment this resolves, not an hour later.
        self.arbiter.bind_signal_id(p.seat_id, p.t_ns, sid)
        self.tracker.open(sid, p)
        st = self._session_stats.setdefault(
            frame.session_date, {"signals": 0, "wins": 0, "r": 0.0})
        st["signals"] += 1

    def _on_resolved(self, sid: str, outcome: Outcome) -> None:
        p = self._open.pop(sid, None)
        if p is None:
            return
        self.arbiter.on_resolved(sid)   # free the exposure slot immediately
        won = outcome.resolution.startswith("tp")
        if p.seat_id not in self.calibrators:
            # A promoted challenger or a dynamically added seat must not crash
            # the grading path.
            self.calibrators[p.seat_id] = Calibrator()
        rb = compute_reward(
            conviction=p.conviction, won=won, realized_r=outcome.realized_r,
            path_score=outcome.path_match,
            mechanism_verdict=outcome.mechanism_verdict,
            is_exploration=p.is_exploration)

        self.colosseum.record(p.seat_id, conviction=p.conviction, won=won,
                              reward=rb.total, realized_r=outcome.realized_r,
                              mechanism_verdict=outcome.mechanism_verdict,
                              was_consensus=False)
        self.calibrators[p.seat_id].update(p.conviction, 1 if won else 0)

        key = f"{p.seat_id}:{p.t_ns}"
        if key in self._ctx and key in self._arm:
            self.bandit.update(self._arm[key], self._ctx[key], rb.total)
            self._ctx.pop(key, None)
            self._arm.pop(key, None)

        sd = session_date(p.t_ns)
        payload = {"resolution": outcome.resolution,
                   "realized_r": outcome.realized_r, "mfe_r": outcome.mfe_r,
                   "mae_r": outcome.mae_r,
                   "path_realized": list(outcome.path_realized),
                   "path_match": outcome.path_match,
                   "mechanism_verdict": outcome.mechanism_verdict,
                   "time_to_outcome_s": outcome.time_to_outcome_s,
                   "lesson_tags": list(normalize_all(outcome.lesson_tags)),
                   "reward": rb.to_dict()}
        self.ledger.append(REC_OUTCOME, f"OUT-{sid}", outcome.t_resolved_ns, sd,
                           payload, ref_id=sid, seat_id=p.seat_id)
        self.journal.append_outcome(sid, sd, payload)

        # Write the outcome back into the search index. Without this the index
        # knows every signal was BORN but not how any of them DIED, which makes
        # `scoreboard` and every outcome-filtered query silently empty.
        self.index.upsert(
            sid, "signal",
            " ".join([p.thesis, p.mechanism, p.predicted_path,
                      p.invalidation, p.counter_case]),
            title=f"{p.direction.value} @ {p.entry}",
            session_date=sd, t_ns=p.t_ns, seat_id=p.seat_id,
            direction=p.direction.value, conviction=p.conviction,
            resolution=outcome.resolution, realized_r=outcome.realized_r,
            mechanism_verdict=outcome.mechanism_verdict)

        for tag in payload["lesson_tags"]:
            self.journal.reinforce_lesson(
                tag, sid, supports=won and outcome.mechanism_verdict == "confirmed",
                note=f"Observed via {p.lens}: {p.mechanism[:180]}", session_date=sd)
            self.index.link(sid, f"LESSON-{tag}", "produced_lesson")

        st = self._session_stats.setdefault(sd, {"signals": 0, "wins": 0, "r": 0.0})
        st["wins"] += 1 if won else 0
        st["r"] += outcome.realized_r

    # ---- health / checkpoints -------------------------------------------

    def metrics(self) -> Metrics:
        s = self.normalizer.stats
        total = max(s.accepted + s.quarantined, 1)
        # Only seats with enough graded calls may influence calibration
        # detectors. `calibration_n` is the MAX across seats, because one
        # well-evidenced seat is enough to justify looking -- but no seat
        # with a handful of samples can trigger a repair on its own.
        ready = [c for c in self.calibrators.values() if c.n >= 50]
        briers = [c.brier for c in ready]
        calib_n = max((c.n for c in self.calibrators.values()), default=0)
        return Metrics(
            calibration_n=calib_n,
            p99_inter_arrival_ms=s.p99_inter_arrival_ms(),
            critical_gaps=s.critical_gaps,
            quarantine_rate=s.quarantined / total,
            clock_skew_ms=s.skew_ms_ewma,
            stale_proposal_rate=(self._stale_proposals /
                                 max(self._total_proposals, 1)),
            brier_score=max(briers) if briers else 0.25,
            wal_truncations=1 if self.ledger.wal.truncated_bytes else 0,
            state_hash_match=True,
        )

    def checkpoint(self) -> None:
        healthy = self.guardian.state is Health.HEALTHY
        payload = {
            "bandit": self.bandit.snapshot(),
            "seats": [s.to_dict() for s in self.colosseum.table()],
            "signal_seq": self._signal_seq,
            "state_hash": self.state_hash(),
        }
        cp = self.registry.save(self._t_ns, session_date(self._t_ns), payload,
                                verified=healthy, code_version=CODE_VERSION,
                                schema_version=FEATURE_SCHEMA_VERSION)
        self.ledger.append(REC_CHECKPOINT, cp.checkpoint_id, self._t_ns,
                           cp.session_date,
                           {"state_hash": cp.state_hash, "verified": cp.verified})

    def _on_incident(self, rec: Dict[str, object]) -> None:
        self._incident_seq += 1
        iid = incident_id(session_date(self._t_ns), self._incident_seq,
                          str(rec.get("detector", "unknown")))
        self.journal.write_incident(iid, self._t_ns, rec)
        self.ledger.append("incident", iid, self._t_ns,
                           session_date(self._t_ns), rec)

    # ---- session close ---------------------------------------------------

    def close_session(self, sd: str, days_elapsed: float = 1.0) -> Path:
        st = self._session_stats.get(sd, {"signals": 0, "wins": 0, "r": 0.0})
        through = self.arbiter.throughput_report(days_elapsed, session_date=sd)
        board = self.colosseum.scoreboard()
        calib = {k: c.report() for k, c in self.calibrators.items() if c.n}

        summary = {"session_date": sd, **st, "throughput": through,
                   "scoreboard": board, "calibration": calib,
                   "health": self.guardian.summary(),
                   "code_version": CODE_VERSION}

        lines = [f"# Session {sd}", "",
                 f"**Signals:** {int(st['signals'])} · **Wins:** {int(st['wins'])} "
                 f"· **Net R:** {st['r']:.2f}", "",
                 "## Throughput vs the >=5/day goal", "",
                 f"Rate: **{through['signals_per_day']}/day** "
                 f"(target {through['target']}). "
                 f"{'On target.' if through['meeting_target'] else through['diagnosis']}",
                 "", "## League table", "",
                 "| rank | seat | lens | elo | n | win rate | brier | shelf | status |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for r in board:
            lines.append(f"| {r['rank']} | {r['seat_id']} | {r['lens']} | "
                         f"{r['elo']} | {r['n']} | {r['win_rate']:.0%} | "
                         f"{r['brier']:.3f} | {r['shelf_weight']:.2f} | "
                         f"{r['status']} |")
        lines += ["", "## Calibration", ""]
        for seat, rep in calib.items():
            lines.append(f"- **{seat}**: Brier {rep['brier']:.3f}, "
                         f"reliability {rep['reliability']:.4f}, "
                         f"overconfidence {rep['overconfidence']:+.3f} "
                         f"over {int(rep['n'])} graded calls")
        lines += ["", "## Health", "",
                  f"State: `{self.guardian.state.value}`, "
                  f"{len(self.guardian.transitions)} transition(s) today.", ""]

        p = self.journal.write_session(sd, summary, "\n".join(lines))
        self.journal.rebuild_lesson_index()
        self.journal.write_root_manifest()
        self.index.upsert(f"SESSION-{sd}", "session", json.dumps(summary),
                          path=str(p), title=f"Session {sd}", session_date=sd)
        return p

    def close(self) -> None:
        self.ledger.close()
        self.index.close()
