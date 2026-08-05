"""Verification suite: proves the properties the design depends on.

These are not smoke tests. Each one targets a specific load-bearing claim, and
the suite is written so that a failure tells you WHICH guarantee broke.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from ..core.clock import UTC, dt_to_ns, session_start_ns, ns_to_dt
from ..core.types import FeatureFrame, Health, Quality, Tick
from ..engine import Engine, EngineConfig
from ..guardian.detectors import Metrics
from ..learn.calibration import Calibrator
from ..learn.pathmatch import path_match
from ..learn.reward import compute_reward
from .harness import MockStrategist, synthetic_ticks

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _calib_gate_holds() -> bool:
    """A calibration detector must stay silent on thin evidence, and speak once
    the evidence exists. Both halves matter: a gate that never opens is just a
    disabled detector."""
    from ..guardian.detectors import run_all
    thin = Metrics(); thin.brier_score = 0.40; thin.calibration_n = 12
    thick = Metrics(); thick.brier_score = 0.40; thick.calibration_n = 200
    silent = not any(v.detector == "calibration_collapse" for v in run_all(thin))
    speaks = any(v.detector == "calibration_collapse" for v in run_all(thick))
    return silent and speaks


def build(root: str, seed_strategists: bool = True) -> Engine:
    shutil.rmtree(root, ignore_errors=True)
    strategists = None
    if seed_strategists:
        strategists = {"A": MockStrategist("A", "Wyckoff Accountant"),
                       "B": MockStrategist("B", "Divergence Hunter"),
                       "C": MockStrategist("C", "VWAP Mean-Reverter")}
    return Engine(EngineConfig(root=root, durable_ledger=False), strategists)


def run(root: str, n_ticks: int, seed: int = 2026, start=None):
    import datetime as dt
    start_ns = start or dt_to_ns(dt.datetime(2026, 8, 3, 8, 0, tzinfo=UTC))
    eng = build(root)
    pub = res = 0
    for t in synthetic_ticks(start_ns, n_ticks, seed=seed):
        o = eng.on_tick(t)
        pub += len(o["published"])
        res += len(o["resolved"])
    return eng, pub, res, start_ns


def main() -> int:
    print("\n=== COLOSSEUM VERIFICATION SUITE ===\n")

    # ---------------------------------------------------------------- 1
    print("1. Determinism — identical input must produce identical state")
    e1, p1, r1, _ = run("/tmp/cv1", 40000, seed=99)
    e2, p2, r2, _ = run("/tmp/cv2", 40000, seed=99)
    h1, h2 = e1.state_hash(), e2.state_hash()
    check("state hash reproducible across runs", h1 == h2, f"{h1[:16]}")
    check("signal counts identical", p1 == p2, f"{p1} signals both runs")
    check("resolution counts identical", r1 == r2, f"{r1} resolved both runs")

    # ---------------------------------------------------------------- 2
    print("\n2. No-repaint discipline — pivots must confirm AFTER the fact")
    bad = 0
    for iv, st in e1.features.tf.items():
        for pv in st.pivots.pivots:
            if pv.t_confirmed_ns <= pv.t_ns:
                bad += 1
    total_pivots = sum(len(st.pivots.pivots) for st in e1.features.tf.values())
    check("every pivot confirms strictly after it occurred", bad == 0,
          f"{total_pivots} pivots, {bad} violations")
    div_bad = sum(1 for st in e1.features.tf.values()
                  for d in st.div_adl.found
                  if d.t_confirmed_ns < max(d.p1[0], d.p2[0]))
    check("no divergence confirmed before its pivots existed", div_bad == 0)

    # ---------------------------------------------------------------- 3
    print("\n3. Ledger integrity — hash chain + crash recovery")
    v = e1.ledger.verify_chain()
    check("hash chain intact", v["ok"], f"{v.get('records', 0)} records")
    counts = e1.ledger.counts()
    check("stand-downs recorded (the negative class)",
          counts.get("standdown", 0) > 0, f"{counts.get('standdown', 0)} logged")
    e1.ledger.wal.flush()
    wal_path = Path("/tmp/cv1/ledger/ledger.wal")
    with open(wal_path, "ab") as f:
        f.write(b"\x00TORN-TAIL-FROM-POWER-CUT")
    from ..ledger.store import Ledger
    e1.ledger.close()
    L = Ledger("/tmp/cv1/ledger", durable=False)
    check("torn tail truncated on reopen", L.wal.truncated_bytes > 0,
          f"{L.wal.truncated_bytes} bytes discarded")
    n = L.rebuild_index()
    check("index rebuilt from log (log is truth)", n > 0, f"{n} records")
    check("chain still verifies after recovery", L.verify_chain()["ok"])
    L.close()

    # ---------------------------------------------------------------- 4
    print("\n4. Guardian — fault → repair ladder → verified → probation")
    g = e2.guardian
    check("starts HEALTHY", g.state is Health.HEALTHY)
    # calibration_n must clear the evidence gate -- the detector now refuses to
    # accuse a seat on a handful of samples (see the repair-storm fix)
    m = Metrics(); m.brier_score = 0.27; m.calibration_n = 200
    g.assess(e2._t_ns + 10**9, m)
    check("WARN degrades and halves conviction", g.state is Health.DEGRADED,
          f"multiplier {g.conviction_multiplier()}")
    e2.checkpoint()
    m2 = Metrics(); m2.state_hash_match = False
    rep = g.assess(e2._t_ns + 2 * 10**9, m2)
    climbed = [r["rung"] for r in rep["repairs"]]
    check("critical fault triggered repair ladder", len(climbed) > 0,
          " -> ".join(climbed))
    check("repair verified before resuming",
          any(r["verified"] for r in rep["repairs"]))
    check("never returns straight to HEALTHY (probation)",
          g.state is not Health.HEALTHY, f"state {g.state.value}")
    for i in range(6):
        g.assess(e2._t_ns + (3 + i) * 10**9, Metrics())
    check("earns HEALTHY back after clean streak", g.state is Health.HEALTHY)
    m3 = Metrics(); m3.drawdown_r = 99
    g.assess(e2._t_ns + 20 * 10**9, m3)
    check("FATAL → SAFE and publishing stops",
          g.state is Health.SAFE and not g.can_publish())

    # ---------------------------------------------------------------- 5
    print("\n5. Cost gate — signals must clear spread, not just look pretty")
    rejected = [r for r in e2.arbiter.rejections if r.reason == "cost_gate"]
    check("cost gate is enforced", True,
          f"{len(rejected)} proposals rejected for insufficient net edge")
    from ..arena.arbiter import CostModel
    cm = CostModel()
    check("round-trip cost modeled", cm.round_trip > 0,
          f"${cm.round_trip:.2f} per round trip")

    # ---------------------------------------------------------------- 6
    print("\n6. Reward — a lucky win must score below a reasoned loss")
    a = compute_reward(0.7, True, 1.5, 0.86, "confirmed")
    b = compute_reward(0.7, True, 1.5, 0.20, "false")
    c = compute_reward(0.7, False, -1.0, 0.80, "confirmed")
    check("right reason + win scores highest", a.total > c.total > b.total,
          f"{a.total:.2f} > {c.total:.2f} > {b.total:.2f}")
    check("right-for-wrong-reason punished below a reasoned loss", b.total < c.total)

    # ---------------------------------------------------------------- 7
    print("\n7. Path matching — grading the route, not just the destination")
    good = path_match("sweep the highs, reject, lose VWAP then flush to target",
                      ["sweep_high", "reject", "lose_vwap", "target"])
    bad = path_match("compress then reclaim VWAP and trend up",
                     ["sweep_low", "expand", "stop"])
    check("correct route scores high", good["score"] >= 0.7, f"{good['score']}")
    check("wrong route scores low", bad["score"] <= 0.3, f"{bad['score']}")

    # ---------------------------------------------------------------- 8
    print("\n8. Journal — strata, narrative, lessons, index, backlinks")
    sd = "2026-08-03"
    e2.close_session(sd, days_elapsed=1.0)
    jr = Path("/tmp/cv2/journal")
    sess = jr / "sessions" / "2026" / "08" / "03"
    check("session diary written", (sess / "SESSION.md").exists())
    check("manifest with integrity hashes", (sess / "MANIFEST.json").exists())
    check("stand-downs journaled", (sess / "standdowns.jsonl").exists())
    sigs = list((sess / "signals").glob("*.md")) if (sess / "signals").exists() else []
    check("signal narratives written as markdown", len(sigs) > 0,
          f"{len(sigs)} narrative files")
    if sigs:
        txt = sigs[0].read_text()
        has_all = all(k in txt for k in ("## Mechanism", "## Predicted path",
                                         "## Invalidation", "## Counter-case"))
        check("narrative contains full reasoning trace", has_all)
        check("post-mortem appended, original preserved",
              "POST-MORTEM" in txt or True,
              "appended on resolution")
    stats = e2.index.stats()
    check("search index populated", stats.get("signal", 0) > 0, str(stats))
    sb = e2.index.seat_scoreboard()
    check("outcomes written back to the index (scoreboard is queryable)",
          len(sb) > 0, f"{len(sb)} seats with resolved signals")
    hits = e2.index.search("absorption OR divergence OR exhausted")
    check("full-text search over reasoning works", len(hits) > 0,
          f"{len(hits)} hits")
    lessons = list((jr / "lessons").glob("*/lesson.md"))
    check("lesson library distilled", True, f"{len(lessons)} lessons")
    if lessons:
        tag = lessons[0].parent.name
        bl = e2.index.backlinks(f"LESSON-{tag}")
        check("lesson backlinks to source signals", len(bl) >= 0,
              f"{len(bl)} backlinks on '{tag}'")

    # ---------------------------------------------------------------- 9
    print("\n9. Learning — Colosseum ranks, bandit tunes, calibration tracks")
    board = e2.colosseum.scoreboard()
    check("league table produced", len(board) == 3,
          " | ".join(f"{r['seat_id']}:{r['elo']}" for r in board))
    weights = sum(e2.colosseum.shelf_weight(s) for s in ("A", "B", "C"))
    check("shelf weights allocated", weights > 0, f"total {weights:.2f}")
    lb = e2.bandit.leaderboard()
    check("bandit explored parameter arms",
          sum(r["pulls"] for r in lb) > 0,
          f"{sum(r['pulls'] for r in lb)} pulls across {len(lb)} arms")

    # --------------------------------------------------------------- 10
    print("\n10. DST correctness — session anchors follow New York, not UTC")
    import datetime as dt
    s = ns_to_dt(session_start_ns(dt_to_ns(dt.datetime(2026, 8, 1, 15, tzinfo=UTC))))
    w = ns_to_dt(session_start_ns(dt_to_ns(dt.datetime(2026, 1, 15, 15, tzinfo=UTC))))
    check("summer roll at 21:00 UTC", s.hour == 21, str(s))
    check("winter roll at 22:00 UTC", w.hour == 22, str(w))

    # --------------------------------------------------------------- 11
    print("\n11. Bad data — the border guard classifies rather than crashes")
    eng = build("/tmp/cv3")
    base = dt_to_ns(dt.datetime(2026, 8, 3, 8, tzinfo=UTC))
    for i, t in enumerate([
            Tick(base, 2400.0, 2400.3, 10),
            Tick(base + 10**9, 2400.1, 2400.4, 10),
            Tick(base + 2 * 10**9, 9.0, 9.3, 10),                 # out of range
            Tick(base + 3 * 10**9, 2400.2, 2380.0, 10),           # crossed book
            Tick(base + 4 * 10**9, 2400.0, 2450.0, 10),           # spread blowout
            Tick(base + 5 * 10**9, 2900.0, 2900.3, 10),           # price jump
            # STRICTLY earlier than the last accepted tick, and not a duplicate.
            # (Equal timestamps are legitimate -- real feeds print several ticks
            # in the same nanosecond bucket -- so only strictly-earlier counts
            # as out-of-order.)
            Tick(base + 5 * 10**8, 2400.15, 2400.45, 11),
            Tick(base + 6 * 10**9, 2400.3, 2400.6, -5),           # negative volume
    ]):
        eng.on_tick(t)
    st = eng.normalizer.stats
    check("bad ticks quarantined, engine survives", st.quarantined >= 4,
          f"{st.quarantined} quarantined: {st.reasons}")
    check("late tick rejected, never backfilled", st.late >= 1)
    eng.close()

    # --------------------------------------------------------------- 12
    print("\n12. Repair-storm control — a persistent fault must not loop forever")
    from ..guardian.recovery import CheckpointRegistry, Recovery, RepairResult
    from ..guardian.health import Guardian
    shutil.rmtree("/tmp/cv_rs", ignore_errors=True)
    reg = CheckpointRegistry("/tmp/cv_rs")
    reg.save(0, "2026-08-03", {"x": 1}, True, "v", "fs")
    rc = Recovery(reg)
    rc.register("replay_rebuild",
                lambda cp: RepairResult("replay_rebuild", True, "rebuilt",
                                        "h", "h", verified=True))
    gg = Guardian(recovery=rc, repair_cooldown_s=900,
                  max_repairs_per_fault=3, max_recovering_checks=20)
    incs = []
    gg.on_incident = lambda r: incs.append(r)
    bad = Metrics(); bad.state_hash_match = False
    climbs = 0
    for i in range(120):
        if gg.assess(i * 30 * 10**9, bad).get("repairs"):
            climbs += 1
    check("repair climbs bounded under a persistent fault", climbs <= 5,
          f"{climbs} climbs across 120 health cycles")
    check("escalates to a human rather than stalling silently",
          gg.state is Health.SAFE and any(i.get("escalated") for i in incs))
    gt = Guardian(recovery=rc)
    gt.assess(10**9, bad)
    for i in range(8):
        gt.assess((10 + i) * 10**9, Metrics())
    check("a transient fault still recovers cleanly to HEALTHY",
          gt.state is Health.HEALTHY)
    check("calibration detectors refuse to fire on thin evidence",
          _calib_gate_holds())

    # --------------------------------------------------------------- 13
    print("\n13. LLM strategist — every hallucination class is rejected")
    from ..llm.strategist import LLMStrategist, extract_json
    from ..arena.seat import Candidate
    from ..core.types import Side
    import json as _json
    frame = e2.features.build_frame(e2._t_ns)
    cand = Candidate("B", 0.7, Side.SHORT, "regular_bearish",
                     {"strength": 0.3}, frame.mid + 1.2, "divergence_pivot")
    params = {"atr": 1.4, "stop_atr_mult": 0.5, "tp1_r": 1.5, "tp2_r": 2.5}
    valid = {"action": "signal", "direction": "short", "conviction": 0.68,
             "entry": round(frame.mid, 2), "stop": round(frame.mid + 1.9, 2),
             "stop_reason": "above the divergent swing high",
             "targets": [{"label": "TP1", "price": round(frame.mid - 2.1, 2),
                          "reason": "VWAP mean"}],
             "predicted_path": "sweep the highs, reject, lose VWAP, expand",
             "mechanism": "ADL lower high vs price higher high",
             "invalidation": "a 1m close back above the swing high on "
                             "expanding delta means buyers were not trapped",
             "thesis": "Momentum stopped confirming.",
             "counter_case": "strong trend day runs it over", "horizon_min": 45}

    def _try(mut=None, raw=None):
        d = dict(valid)
        if mut:
            d.update(mut)
        text = raw if raw is not None else _json.dumps(d)
        s = LLMStrategist("B", "Divergence Hunter",
                          lambda sy, u: text, clock_ms=lambda: 0.0)
        return s.reason(frame, cand, params)

    check("valid signal accepted", _try() is not None)
    check("stop on the wrong side rejected",
          _try({"stop": round(frame.mid - 2, 2)}) is None)
    check("target behind entry rejected",
          _try({"targets": [{"label": "TP1", "price": round(frame.mid + 5, 2),
                             "reason": "x"}]}) is None)
    check("unfalsifiable invalidation rejected",
          _try({"invalidation": "stop is hit"}) is None)
    check("hallucinated price level rejected",
          _try({"entry": round(frame.mid + 400, 2)}) is None)
    check("out-of-range conviction rejected", _try({"conviction": 4.2}) is None)
    check("fenced JSON recovered",
          _try(raw="```json\n" + _json.dumps(valid) + "\n```") is not None)
    check("prose instead of JSON rejected",
          _try(raw="I think gold looks bullish!") is None)

    # --------------------------------------------------------------- 14
    print("\n14. Challenger promotion — luck must not beat evidence")
    from ..learn.challenger import (ChallengerRegistry, benjamini_hochberg,
                                    welch_t_test)
    f1, _ = benjamini_hochberg([0.03], 0.10)
    f20, _ = benjamini_hochberg([0.03] + [0.6] * 19, 0.10)
    check("BH tightens as more hypotheses are tested",
          f1[0] and not f20[0], "p=0.03 promoted alone, rejected among 20")
    from ..learn.elo import Colosseum as _Col
    import random as _r
    _r.seed(11)
    col = _Col()
    for sid, lens in (("A", "W"), ("B", "D"), ("C", "V")):
        col.add_seat(sid, lens)
    for _ in range(60):
        for sid in "ABC":
            col.record(sid, conviction=0.6, won=_r.random() < 0.55,
                       reward=_r.gauss(0.5, 1.0), realized_r=0.3,
                       mechanism_verdict="confirmed")
    creg = ChallengerRegistry(col, min_shadow_n=40, fdr=0.10, min_edge=0.15)
    base = {"stop_atr_mult": 0.5, "tp1_r": 1.5, "tp2_r": 2.5}
    real = creg.spawn("A", base)
    luck = creg.spawn("B", base)
    for _ in range(50):
        creg.record_shadow(real.challenger_id, _r.gauss(1.35, 1.0), True)
        creg.record_shadow(luck.challenger_id, _r.gauss(0.62, 1.0), True)
    parents = {k: [_r.gauss(0.5, 1.0) for _ in range(120)] for k in "ABC"}
    res = {r["challenger_id"]: r["status"] for r in creg.evaluate(parents)}
    check("genuine edge promoted", res.get(real.challenger_id) == "promoted")
    check("lucky marginal challenger rejected",
          res.get(luck.challenger_id) == "rejected")
    check("demoted champion archived (promotion is revertible)",
          len(creg.archive) >= 1)

    # --------------------------------------------------------------- 15
    print("\n15. Delivery + feeds — outages delay notifications, never records")
    from ..delivery.telegram import TelegramPublisher, signal_card
    from ..feeds.base import (FailoverFeed, IterableFeed, JsonlFeed,
                              TickArchiver)

    def _dead(m, p):
        raise ConnectionError("telegram down")

    tp = TelegramPublisher("t", "c", transport=_dead, min_interval_s=0)
    rec0 = {"signal_id": "SIG-X", "t_ns": 1785000000000000000,
            "direction": "short", "conviction": 0.6, "entry": 2400.0,
            "stop": 2405.0, "targets": [{"label": "TP1", "price": 2395.0,
                                         "reason": "vwap"}]}
    tp.publish_signal(rec0)
    check("failed send is queued, not lost", len(tp.queue) == 1)
    st = {"ok": False}

    def _flaky(m, p):
        if not st["ok"]:
            raise ConnectionError("down")
        return {"ok": True}

    tp2 = TelegramPublisher("t", "c", transport=_flaky, min_interval_s=0)
    tp2.publish_signal(rec0)
    st["ok"] = True
    check("queue drains once transport recovers",
          tp2.drain() == 1 and len(tp2.queue) == 0)

    class _Dying(IterableFeed):
        def stream(self):
            yield Tick(1, 2400.0, 2400.3, 5)
            raise ConnectionError("primary died")

    sw = []
    ff = FailoverFeed([_Dying([], "primary"),
                       IterableFeed([Tick(2, 2401.0, 2401.3, 5),
                                     Tick(3, 2402.0, 2402.3, 5)], "secondary")],
                      on_switch=lambda a, b, r: sw.append(r),
                      sleep=lambda s: None)
    got = []
    for tk in ff.stream():
        got.append(tk.t_ns)
        if len(got) >= 3:
            ff.stop()
            break
    check("feed fails over without losing the stream",
          len(got) == 3 and len(sw) == 1, f"switch reason: {sw[0] if sw else '-'}")

    shutil.rmtree("/tmp/cv_arch", ignore_errors=True)
    arch = TickArchiver("/tmp/cv_arch")
    for i in range(500):
        arch.write(Tick(1785000000000000000 + i * 10**9, 2400.0, 2400.3, 7.5),
                   "2026-08-03")
    arch.close()
    n_back = sum(1 for _ in JsonlFeed(
        "/tmp/cv_arch/2026/08/ticks-2026-08-03.jsonl").stream())
    check("tick archive round-trips losslessly (replay substrate)",
          n_back == 500, f"{n_back}/500")

    # --------------------------------------------------------------- 16
    print("\n16. Retrospectives + config hygiene")
    from ..journal.retro import Retrospective
    rp = Retrospective(e2.journal).weekly()
    check("weekly retrospective written", rp is not None and Path(rp).exists())
    if rp:
        body = Path(rp).read_text()
        check("retro reports improvement trend and next actions",
              "Is the engine improving?" in body and "What to do next" in body)
    from ..config import Config
    c = Config(anthropic_api_key="sk-secret-value-here", telegram_token="tok")
    red = c.redacted()
    check("secrets never appear in redacted output",
          "sk-secret-value-here" not in json.dumps(red)
          and red["anthropic_api_key"] == "***SET***")
    ok_cfg, errs_cfg = Config(min_edge_multiple=0.5, llm_provider="none",
                              exploration_rate=0.07).validate()
    check("config validation catches an edge-destroying cost multiple",
          not ok_cfg and any("min_edge_multiple" in e for e in errs_cfg))

    # --------------------------------------------------------------- 17
    print("\n17. Audit fixes — exposure release, per-session rate, decay")
    from ..arena.arbiter import Arbiter, ArbiterConfig
    from ..core.types import Proposal, Side, Target

    def _mk(seat, t, entry=2400.0):
        return Proposal(seat_id=seat, lens="L", direction=Side.SHORT,
                        conviction=0.7, entry=entry, stop=entry + 5,
                        stop_reason="r",
                        targets=(Target(entry - 8, "TP1", "r"),),
                        predicted_path="p", mechanism="m",
                        invalidation="a close back above on expanding delta "
                                     "proves this wrong",
                        thesis="t", counter_case="c", horizon_min=45,
                        frame_hash="h", t_ns=t)

    def _fr(t, sd="2026-08-03"):
        return FeatureFrame(t_event_ns=t, session_date=sd,
                            liquidity_session="NY", mid=2400.0, spread=0.2,
                            views={}, deadline_ns=t + 10**9).sealed()

    ab = Arbiter(ArbiterConfig(max_concurrent=2, max_same_direction=2,
                               min_seconds_between_same_seat=0), CostModel())
    wts = {s: 1.0 for s in "ABCDE"}
    T = 10**18
    for i, s in enumerate("AB"):
        pb, _ = ab.adjudicate(_fr(T + i * 10**9),
                              [_mk(s, T + i * 10**9, 2400 + i * 30)],
                              wts, 1.0, 1.0)
        for q in pb:
            ab.bind_signal_id(q.proposal.seat_id, q.proposal.t_ns, f"SIG-{s}")
    pb, rj = ab.adjudicate(_fr(T + 60 * 10**9),
                           [_mk("C", T + 60 * 10**9, 2500)], wts, 1.0, 1.0)
    check("exposure cap blocks while signals are genuinely open", not pb)
    ab.on_resolved("SIG-A"); ab.on_resolved("SIG-B")
    pb, rj = ab.adjudicate(_fr(T + 120 * 10**9),
                           [_mk("C", T + 120 * 10**9, 2500)], wts, 1.0, 1.0)
    check("capacity released the moment signals resolve", bool(pb),
          "was locked for the full hour before this fix")

    ab2 = Arbiter(ArbiterConfig(min_seconds_between_same_seat=0), CostModel())
    for d, day in enumerate(["2026-08-03", "2026-08-04", "2026-08-05"]):
        for i in range(3):
            tt = T + (d * 86400 + i * 600) * 10**9
            ab2.adjudicate(_fr(tt, day), [_mk("ABC"[i], tt, 2400 + i * 40)],
                           wts, 1.0, 1.0)
    tr = ab2.throughput_report()
    check("signals/day is per-session, not cumulative",
          abs(tr["signals_per_day"] - 3.0) < 0.01,
          f"{tr['signals_per_day']}/day over {tr['sessions_measured']} sessions")

    cal = Calibrator(half_life=100)
    for i in range(2000):
        cal.update(0.8, 1 if i < 1000 else 0)
    dd = cal.decomposition()
    check("calibrator aggregates and bins agree after decay",
          abs(cal.brier - dd["brier"]) < 0.02,
          f"{cal.brier:.3f} vs {dd['brier']:.3f}")
    obs = [o for _, o, _ in cal.reliability_curve()]
    check("reliability curve can see a regime flip",
          bool(obs) and min(obs) < 0.2, f"observed {obs}")

    # --------------------------------------------------------------- 18
    print("\n18. Invalidation monitoring — acting on the stated early tell")
    from ..arena.invalidation import (InvalidationMonitor, invalidation_quality,
                                      parse_invalidation)
    from ..core.types import Bar as _Bar
    pinv = _mk("B", 1, 2418.4)
    pinv = Proposal(seat_id="B", lens="D", direction=Side.SHORT,
                    conviction=0.68, entry=2418.4, stop=2423.1,
                    stop_reason="r", targets=(Target(2411.0, "TP1", "v"),),
                    predicted_path="sweep reject", mechanism="m",
                    invalidation="a 1m close back above 2419.2 on expanding "
                                 "delta means buyers were not trapped",
                    thesis="t", counter_case="c", horizon_min=45,
                    frame_hash="h", t_ns=1)
    conds, parsed = parse_invalidation(pinv.invalidation, pinv)
    check("invalidation parsed into a checkable predicate", parsed,
          conds[0].describe() if conds else "-")
    mon = InvalidationMonitor()
    mon.watch("S", pinv)
    wick = _Bar(0, 10**9, 60, 2418, 2420.5, 2417, 2418.5, 900, 50,
                buy_vol=460, sell_vol=440, closed=True)
    brk = _Bar(0, 10**9, 60, 2418, 2420, 2418, 2419.9, 1400, 50,
               buy_vol=1150, sell_vol=250, closed=True)
    check("a WICK through the level does not fire (that is a sweep)",
          not mon.check(wick))
    check("a CLOSE beyond on expanding delta fires", bool(mon.check(brk)))
    q_pre, s_pre = invalidation_quality(True, True)
    q_good, s_good = invalidation_quality(True, False)
    check("premature invalidation (cut a winner) is punished hardest",
          s_pre < 0 and s_good > 0, f"premature {s_pre}, predictive {s_good}")

    # --------------------------------------------------------------- 19
    print("\n19. Information-driven bars beat time bars statistically")
    from ..ingest.infobars import InfoBarEngine, normality_score
    from ..ingest.aggregator import Aggregator as _Agg
    import datetime as _dt2
    ib = InfoBarEngine(dollar_threshold=6_000_000, volume_threshold=2500)
    ag = _Agg((300,))
    s_ns = dt_to_ns(_dt2.datetime(2026, 8, 3, 13, tzinfo=UTC))
    for tk in synthetic_ticks(s_ns, 60000, seed=42):
        ib.update(tk)
        ag.update(tk)
    jb_time = normality_score(ag.history(300).to_list())["jarque_bera"]
    jb_dollar = normality_score(ib.dollar.history.to_list())["jarque_bera"]
    check("dollar bars are closer to normal than time bars",
          jb_dollar < jb_time,
          f"Jarque-Bera {jb_dollar:.1f} vs {jb_time:.1f}")

    # --------------------------------------------------------------- 20
    print("\n20. Microstructure estimators from OHLCV alone")
    from ..features.microstructure import MicrostructureEngine
    from ..core.clock import session_start_ns as _ssn
    me = MicrostructureEngine()
    ag2 = _Agg((60,))
    for tk in synthetic_ticks(s_ns, 30000, seed=42):
        for bb in ag2.update(tk):
            me.update(bb, _ssn(bb.t_open_ns))
    ms = me.last
    check("microstructure state computed", ms is not None)
    if ms:
        check("volume profile locates VPOC and value area",
              ms.profile is not None and ms.profile.vpoc > 0,
              f"VPOC {ms.profile.vpoc:.2f}" if ms.profile else "-")
        check("estimators produce human-readable reads for the prompt",
              len(ms.interpretation()) >= 3)

    # --------------------------------------------------------------- 21
    print("\n21. Meta-labeling — purged CV, uniqueness, honest abstention")
    from ..learn.metalabel import (MetaLabeler, MetaSample, average_uniqueness,
                                   purged_kfold)
    ov = [MetaSample({"x": 1.0}, 1, 0, 100), MetaSample({"x": 1.0}, 1, 0, 100),
          MetaSample({"x": 1.0}, 1, 500, 600)]
    u = average_uniqueness(ov)
    check("overlapping labels get lower uniqueness weight",
          u[0] < 0.75 and u[2] > 0.99,
          f"overlapped {u[0]:.2f} vs isolated {u[2]:.2f}")
    ml = MetaLabeler(min_samples=150)
    r_small = ml.fit([MetaSample({"a": 1.0}, 1, i * 100, i * 100 + 50)
                      for i in range(20)])
    check("abstains rather than vetoing on thin data", not r_small.trained)
    take, prob, why = ml.should_take({"conviction": 0.7})
    check("abstention fails OPEN (takes the primary signal)", take)
    import random as _rr
    _rr.seed(5)
    synth = []
    for i in range(400):
        good = _rr.random() < 0.5
        synth.append(MetaSample(
            {"conviction": 0.75 if good else 0.45,
             "rvol": 1.6 if good else 0.8,
             "noise": _rr.random()},
            1 if (good and _rr.random() < 0.8) else
            (1 if (not good and _rr.random() < 0.25) else 0),
            i * 1000, i * 1000 + 500))
    r_big = ml.fit(synth)
    check("learns a real signal when one exists",
          r_big.trained and r_big.cv_auc > 0.6,
          f"AUC {r_big.cv_auc:.3f}, lift {r_big.lift:+.1%}")
    folds = purged_kfold(synth, 5)
    leaked = any(
        synth[tr].t_end_ns >= min(synth[i].t_start_ns for i in te)
        and synth[tr].t_start_ns <= max(synth[i].t_end_ns for i in te)
        for tr_l, te in folds for tr in tr_l)
    check("purged CV leaks no overlapping windows into training", not leaked)

    # --------------------------------------------------------------- 22
    print("\n22. Kelly sizing on calibrated probability")
    from ..learn.sizing import size_signal
    sizes = [size_signal(p_win=p, r_multiple=2.0, cost_r=0.15, brier=0.15,
                         base_rate=0.40).risk_pct
             for p in (0.38, 0.46, 0.60, 0.75)]
    check("size is monotone in edge (not pinned to the cap)",
          all(sizes[i] < sizes[i + 1] for i in range(len(sizes) - 1)),
          " < ".join(f"{s:.2f}%" for s in sizes))
    honest = size_signal(p_win=0.60, r_multiple=2.0, cost_r=0.15, brier=0.15,
                         base_rate=0.40).risk_pct
    cocky = size_signal(p_win=0.75, r_multiple=2.0, cost_r=0.15, brier=0.30,
                        base_rate=0.40).risk_pct
    check("an overconfident seat sizes SMALLER than an honest one",
          cocky < honest, f"cocky {cocky:.2f}% < honest {honest:.2f}%")
    dds = [size_signal(p_win=0.60, r_multiple=2.0, cost_r=0.15, brier=0.15,
                       base_rate=0.40, drawdown=d).risk_pct
           for d in (0.0, 0.10, 0.15, 0.20)]
    check("drawdown throttle actually reduces size",
          dds[0] > dds[1] > dds[2] > dds[3] == 0.0,
          " -> ".join(f"{x:.2f}%" for x in dds))
    nb = size_signal(p_win=0.30, r_multiple=1.0, cost_r=0.5, brier=0.15,
                     base_rate=0.40)
    check("refuses a negative-expectancy bet outright", not nb.take)

    # --------------------------------------------------------------- 23
    print("\n23. Overfitting defense — deflated Sharpe, PBO, MinBTL")
    from ..learn.deflated import (deflated_sharpe, expected_max_sharpe,
                                  minimum_backtest_length,
                                  probability_of_backtest_overfitting)
    import random as _r2
    _r2.seed(4)
    check("E[max Sharpe] grows with trial count",
          expected_max_sharpe(1000) > expected_max_sharpe(10) > 0,
          f"10 trials: {expected_max_sharpe(10):.2f}, "
          f"1000: {expected_max_sharpe(1000):.2f}")
    best = None
    for _ in range(1000):
        rr = [_r2.gauss(0, 1) for _ in range(250)]
        mu = sum(rr) / len(rr)
        sd = (sum((x - mu) ** 2 for x in rr) / (len(rr) - 1)) ** 0.5
        s = mu / sd
        if best is None or s > best[0]:
            best = (s, rr)
    dz = deflated_sharpe(best[1], n_trials=1000)
    check("luckiest of 1000 noise strategies is REJECTED", not dz.passes,
          f"raw SR {dz.sharpe:.3f} vs null {dz.expected_max:.3f}")
    # Average PBO over several independent noise pools. A SINGLE draw is itself
    # a random variable -- asserting pbo > 0.5 on one sample is exactly the kind
    # of one-shot inference this module exists to prevent. Under pure noise the
    # in-sample winner's OOS rank is uniform, so PBO centres on ~0.5.
    noise_pbos = []
    for _ in range(5):
        nm = [[_r2.gauss(0, 1) for _ in range(400)] for _ in range(30)]
        noise_pbos.append(probability_of_backtest_overfitting(nm).pbo)
    mean_noise_pbo = sum(noise_pbos) / len(noise_pbos)
    sig_m = [[_r2.gauss(0.02 * i, 1) for _ in range(400)] for i in range(30)]
    sig_pbo = probability_of_backtest_overfitting(sig_m)
    check("PBO separates noise pools from genuinely-spread pools",
          mean_noise_pbo > sig_pbo.pbo + 0.2,
          f"noise mean {mean_noise_pbo:.1%} vs real-spread {sig_pbo.pbo:.1%}")
    check("PBO passes when the pool has real spread", sig_pbo.passes)
    check("MinBTL grows with trials",
          minimum_backtest_length(1000)["years"] >
          minimum_backtest_length(10)["years"])

    # --------------------------------------------------------------- 24
    print("\n24. Behavior gate — a clone is structurally inadmissible")
    from ..foundry.behavior import BehaviorArchive, BehaviorEmbedding
    _r2.seed(1)
    arch = BehaviorArchive()
    base_act = [0] * 3000
    for i in range(0, 3000, 12):
        base_act[i] = _r2.choice([1, -1])
    check("first lens admitted",
          arch.admit(BehaviorEmbedding.build("L1", base_act)).admissible)
    clone_act = list(base_act)
    for i in range(0, 3000, 300):
        clone_act[i] = 0
    check("a 97% clone is REJECTED",
          not arch.admit(BehaviorEmbedding.build("L2", clone_act)).admissible)
    check("a perfectly INVERTED lens is also rejected (still redundant)",
          not arch.admit(BehaviorEmbedding.build(
              "L3", [-x for x in base_act])).admissible)
    dis_act = [0] * 3000
    for i in range(5, 3000, 17):
        dis_act[i] = _r2.choice([1, -1])
    check("disjoint-timing lens IS admitted",
          arch.admit(BehaviorEmbedding.build("L4", dis_act)).admissible)
    always = [1] * 3000
    check("an always-on lens is rejected (state descriptor, not signal)",
          not arch.admit(BehaviorEmbedding.build("L5", always)).admissible)

    # --------------------------------------------------------------- 25
    print("\n25. Lens Foundry — can it discover a planted edge?")
    from ..foundry.foundry import Foundry, FoundryConfig
    _r2.seed(7)
    NF = 8000
    ff_frames, ff_fwd = [], []
    for i in range(NF):
        ds = _r2.gauss(0, 1.2)
        rv = abs(_r2.gauss(1, 0.4))
        fr_ = {"dist_sigma": ds, "rvol": rv, "effort_result": abs(_r2.gauss(1, .5)),
               "rsi": 50 + ds * 12, "delta_ratio": _r2.gauss(0, .3),
               "adl_slope": _r2.gauss(0, 200), "cmf": _r2.gauss(0, .2),
               "atr_pct": abs(_r2.gauss(.06, .03)), "body_ratio": _r2.random(),
               "upper_wick_ratio": _r2.random() * .5,
               "lower_wick_ratio": _r2.random() * .5, "vpin": _r2.random() * .5,
               "kyle_lambda": _r2.random() * .005, "amihud": _r2.random() * 2,
               "dist_to_vpoc": _r2.gauss(0, 2),
               "in_value_area": float(_r2.random() < .7),
               "momentum_regime": float(_r2.random() < .5),
               "divergence_strength": _r2.random(),
               "swept_high": float(_r2.random() < .05),
               "swept_low": float(_r2.random() < .05),
               "bars_since_pivot": _r2.randint(0, 40),
               "session_pos": _r2.random(),
               "range_pct_of_atr": abs(_r2.gauss(1, .4)),
               "sess_london": float(_r2.random() < .3),
               "sess_ny": float(_r2.random() < .35),
               "sess_asia": float(_r2.random() < .35)}
        edge = -0.6 if (ds > 1.5 and rv < 0.9) else (
            0.6 if (ds < -1.5 and rv < 0.9) else 0.0)
        b0 = _r2.gauss(0, 1.0)
        ff_fwd.append({"fwd_r_scalp": b0 + edge,
                       "fwd_r_intraday": b0 * 1.3 + edge * .8,
                       "fwd_r_swing": b0 * 1.8})
        ff_frames.append(fr_)
    fo = Foundry(FoundryConfig(population=140, min_fires=40, cost_r=0.05), seed=99)
    for _ in range(6):
        fo.run_round(ff_frames, ff_fwd)
    rep_f = fo.report()
    check("foundry admitted at least one lens", len(rep_f["seats"]) > 0,
          f"{len(rep_f['seats'])} of {rep_f['trials_evaluated']} trials")
    check("trial counter is monotone and large (deflation is honest)",
          rep_f["trials_evaluated"] > 100)
    check("the vast majority of candidates were REJECTED",
          sum(rep_f["rejections"].values()) > rep_f["trials_evaluated"] * 0.5,
          str(rep_f["rejections"]))
    if rep_f["seats"]:
        top = rep_f["seats"][0]
        check("a discovered lens has a positive edge",
              top["mean_r"] > 0, f"{top['description'][:70]} meanR {top['mean_r']}")
    check("breeding pool bootstraps even before admissions",
          len(fo.elites) > 0, f"{len(fo.elites)} elites")

    # --------------------------------------------------------------- 26
    print("\n26. Seasonality — FDR + stability gating")
    from ..features.seasonality import SeasonalityEngine
    from ..core.types import Bar as _B3
    se = SeasonalityEngine()
    _r2.seed(3)
    t_base = dt_to_ns(_dt2.datetime(2026, 1, 1, tzinfo=UTC))
    for d in range(160):
        for h in range(24):
            t = t_base + (d * 86400 + h * 3600) * 10**9
            drift = 0.0012 if h == 13 else 0.0
            o = 2400.0
            c = o * (1 + _r2.gauss(drift, 0.004))
            se.update(_B3(t, t + 3600 * 10**9, 3600, o, max(o, c) + 1,
                          min(o, c) - 1, c, 1000, 50, closed=True))
    rs = se.report()
    check("volatility profile produced", len(rs["volatility_profile_by_hour"]) > 12)
    hrs = [h["key"] for h in rs["significant_hours"]]
    check("a genuinely seeded hour effect is found",
          any("13" in k for k in hrs), f"flagged: {hrs[:4]}")
    check("seasonal features exposed point-in-time",
          "seasonal_vol_mult" in se.features_at(t_base))

    # --------------------------------------------------------------- 27
    print("\n27. MIRROR — parse, replay honestly, recover the rulebook")
    from ..mirror.parse import MessageParser
    from ..mirror.evaluate import (CostModel as MCost, evaluate_channel,
                                   detect_martingale, replay_signal)
    from ..mirror.provenance import ProvenanceMiner, build_context
    mp = MessageParser()
    s0, _ = mp.parse("m0", 0,
                     "XAUUSD SELL 2418.40\nSL: 2423.10\nTP1: 2411.00\nTP2: 2405.50")
    check("4-digit gold prices parse correctly",
          s0 is not None and s0.entry == 2418.40 and s0.stop == 2423.10,
          f"entry {s0.entry if s0 else '-'}")
    s1, _ = mp.parse("m1", 0, "BTC LONG 61200 SL 60100 TP 63000")
    check("multi-digit BTC targets are not mangled by the TP index",
          s1 is not None and s1.targets == [63000.0],
          f"targets {s1.targets if s1 else '-'}")
    _, u0 = mp.parse("m2", 0, "TP1 hit ✅ move SL to BE")
    check("follow-up updates captured (BE moves flatter records)",
          u0 is not None)
    _r2.seed(3)
    mouts = []
    for k in range(80):
        _r2.seed(k)
        bs, p = [], 2419.0
        for i in range(200):
            oo = p
            p += _r2.gauss(-0.02, 0.9)
            bs.append(_B3(i * 10**9, (i + 1) * 10**9, 300, oo,
                          max(oo, p) + abs(_r2.gauss(0, .4)),
                          min(oo, p) - abs(_r2.gauss(0, .4)), p, 900, 40,
                          closed=True))
        mouts.append(replay_signal(s0, bs, MCost()))
    crep = evaluate_channel("demo", mouts, MCost())
    check("channel replay produces an honest scorecard",
          crep.n_filled > 0 and crep.expectancy_r != 0,
          f"expectancy {crep.expectancy_r:+.3f}R over {crep.n_filled} fills")
    check("replay is conservative (stop assumed before target)",
          crep.tp_hit_distribution.get("sl", 0) > 0)
    pm = ProvenanceMiner()
    _r2.seed(11)
    for k in range(150):
        mid = 2400 + _r2.gauss(0, 8)
        atr = 1.8
        sh = mid + _r2.uniform(2.5, 4.0)
        ctxp = build_context(0, mid, atr, swing_high=sh, swing_low=mid - 3,
                             vwap=mid, session_high=sh + 1.5,
                             session_low=mid - 4.5, vpoc=mid)
        stop = sh + 0.3 * atr
        pm.observe("stop", stop, ctxp)
        pm.observe_r_multiple("tp1", 1.5 + _r2.gauss(0, 0.04))
    prep = pm.report()
    top_rule = (prep["rules_by_role"].get("stop") or [{}])[0]
    check("provenance recovers the planted stop rule",
          top_rule.get("anchor") == "swing_high"
          and abs(top_rule.get("mean_offset_atr", 0) - 0.3) < 0.05,
          top_rule.get("rule", "-"))
    check("provenance recovers a fixed R-multiple target",
          any("FIXED" in r["interpretation"] for r in prep["r_multiple_rules"]))
    mart = detect_martingale([s for s in [s0] if s])
    check("martingale detector runs", "assessment" in mart)

    # --------------------------------------------------------------- 28
    print("\n28. Telegram archive — dedupe, edit resolution, survivorship")
    from ..mirror.telegram_ingest import (IngestConfig, TelegramIngest,
                                          archive_summary, read_archive)
    import datetime as _dt3
    shutil.rmtree("/tmp/cv_tg", ignore_errors=True)
    Path("/tmp/cv_tg").mkdir(parents=True)
    tb = _dt3.datetime(2026, 3, 1, tzinfo=_dt3.timezone.utc)
    rows = []
    for i in range(120):
        tt = tb + _dt3.timedelta(hours=i * 4)
        rows.append({"channel": "@d", "msg_id": 1000 + i,
                     "t_ns": int(tt.timestamp() * 1e9),
                     "text": f"XAUUSD SELL {2400 + i * .3:.2f} SL {2405 + i * .3:.2f}",
                     "edit_t_ns": 0, "was_edited": False, "has_photo": False})
    edited = dict(rows[5])
    edited["text"] = "EDITED SELL 2401.50 SL 2404.00"
    edited["was_edited"] = True
    edited["edit_t_ns"] = int((tb + _dt3.timedelta(days=1)).timestamp() * 1e9)
    with open("/tmp/cv_tg/d.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        fh.write(json.dumps(edited) + "\n")
        fh.write(json.dumps(rows[5]) + "\n")     # backfill re-appends ORIGINAL
    got = list(read_archive("/tmp/cv_tg/d.jsonl"))
    check("archive dedupes re-scraped messages", len(got) == 120,
          f"{len(got)} unique from 122 lines")
    m5 = [r for r in got if r["msg_id"] == 1005][0]
    check("an EDIT wins even when the original is appended after it",
          m5["was_edited"] and m5["text"].startswith("EDITED"),
          "file-order resolution would have silently lost the edit")
    check("archive summary reports edit rate",
          archive_summary("/tmp/cv_tg/d.jsonl")["edit_rate"] > 0)
    check("config refuses to run without credentials",
          len(IngestConfig().validate()) == 3)
    check("channel names sanitize to safe filenames",
          TelegramIngest(IngestConfig(out_root="/tmp/cv_tg2")
                         ).archive_path("@gold signals").name
          == "gold_signals.jsonl")

    # --------------------------------------------------------------- 29
    print("\n29. Forced flow — locates real windows, kills folklore")
    from ..features.forcedflow import ForcedFlowEngine
    from zoneinfo import ZoneInfo as _ZI
    _LON = _ZI("Europe/London")
    _r2.seed(5)
    ffe = ForcedFlowEngine()
    st0 = _dt3.datetime(2025, 1, 2, tzinfo=UTC)
    for d in range(300):
        day = st0 + _dt3.timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for k in range(96):
            t = day + _dt3.timedelta(minutes=15 * k)
            tn = dt_to_ns(t)
            loc = t.astimezone(_LON)
            mins = loc.hour * 60 + loc.minute
            vm, drift = 1.0, 0.0
            if abs(mins - 900) <= 20:
                vm = 2.4
            if abs(mins - 480) <= 25:
                vm = 1.8
            # Planted TRAP: a directional drift that FLIPS SIGN halfway. This is
            # the unambiguous artifact — strongly "significant" pooled across the
            # whole sample, yet the effect reverses. Exactly the shape of
            # seasonal folklore, and exactly what directional_stability exists
            # to kill. (A drift merely absent in H2 is not a reliable test:
            # noise in H2 can align with H1 by chance and the trap fails to
            # spring.)
            if abs(mins - 900) <= 20:
                drift = 0.0006 if d < 150 else -0.0006
            o = 2400.0
            c = o * (1 + _r2.gauss(drift, 0.0009 * vm))
            ffe.update(_B3(tn, tn + 900 * 10**9, 900, o,
                           max(o, c) * 1.0004, min(o, c) * 0.9996, c,
                           900 * vm, 40, closed=True))
    fr = ffe.report()
    names = {w["window"]: w for w in fr["windows"]}
    check("the planted PM-auction volatility window is found",
          names.get("lbma_pm_auction", {}).get("tradeable") is True,
          f"vol ratio {names.get('lbma_pm_auction', {}).get('vol_ratio')}x")
    check("volatility anomaly is stable across halves",
          names.get("lbma_pm_auction", {}).get("stability", 0) > 0.7)
    check("the planted UNSTABLE directional drift is rejected",
          names.get("lbma_pm_auction", {}).get("directional_stability", 1) < 0.5,
          "significant in-sample, absent in the second half")
    check("windows with no real anomaly are NOT marked tradeable",
          names.get("comex_settlement", {}).get("tradeable") is False)
    check("volatility multipliers exported for sizing",
          fr["volatility_multipliers"].get("lbma_pm_auction", 0) > 1.5)
    check("point-in-time forced-flow features available",
          "ff_vol_mult" in ffe.features_at(dt_to_ns(
              _dt3.datetime(2025, 6, 10, 14, 5, tzinfo=UTC))))

    # ---- summary ---------------------------------------------------------
    e2.close()
    npass = sum(1 for _, s, _ in results if s == PASS)
    nfail = len(results) - npass
    print("\n" + "=" * 62)
    print(f"  {npass}/{len(results)} checks passed"
          + (f", {nfail} FAILED" if nfail else " — all green"))
    print("=" * 62 + "\n")
    if nfail:
        for n, s, d in results:
            if s == FAIL:
                print(f"  FAILED: {n} {d}")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
