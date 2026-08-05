"""Command line: the operator's interface.

    python -m colosseum.cli run          --config cfg.json
    python -m colosseum.cli bootstrap    --archive ./ticks --days 30
    python -m colosseum.cli verify-state --archive ./ticks
    python -m colosseum.cli retro        --weekly | --monthly
    python -m colosseum.cli search       "absorption at the highs"
    python -m colosseum.cli scoreboard
    python -m colosseum.cli doctor
    python -m colosseum.cli selftest
    python -m colosseum.cli simulate     --ticks 40000

START_HERE Steps 3-5, each one command:

    python -m colosseum.cli verdict    --archive mirror_data/raw/ch.jsonl \
                                       --bars gold_1m.csv --json
    python -m colosseum.cli rulebook   --archive mirror_data/raw/ch.jsonl \
                                       --bars gold_1m.csv
    python -m colosseum.cli forcedflow --bars gold_15m.csv

`doctor` is the one to run first on any new deployment: it validates config,
checks ledger integrity, and reports what is wired versus what is still a stub,
so a misconfiguration surfaces at setup rather than at 3am.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional

from .config import Config
from .engine import Engine, EngineConfig
from .arena.arbiter import CostModel


def _engine(cfg: Config, strategists: Optional[Dict] = None) -> Engine:
    return Engine(EngineConfig(
        root=cfg.root, intervals=tuple(cfg.intervals),
        signal_interval_for_frames=cfg.signal_timeframe_s,
        frame_every_s=cfg.frame_every_s,
        health_every_s=cfg.health_every_s,
        checkpoint_every_s=cfg.checkpoint_every_s,
        durable_ledger=cfg.durable_ledger,
        cost=CostModel(spread=cfg.spread,
                       commission_per_unit=cfg.commission_per_unit,
                       slippage=cfg.slippage,
                       min_edge_multiple=cfg.min_edge_multiple),
    ), strategists or {})


def _build_strategists(cfg: Config, engine: Engine) -> Dict:
    """Wire real LLM seats if a provider is configured; otherwise report clearly
    that the engine will observe and journal but never reason."""
    from .llm.strategist import (LLMStrategist, anthropic_completer,
                                 openai_completer)
    from .llm.prompt import LENSES

    if cfg.llm_provider == "none":
        return {}
    try:
        if cfg.llm_provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
            complete = anthropic_completer(client, cfg.llm_model)
        elif cfg.llm_provider == "openai":
            import openai
            client = openai.OpenAI(api_key=cfg.openai_api_key)
            complete = openai_completer(client, cfg.llm_model)
        else:
            print(f"unknown llm_provider {cfg.llm_provider!r}", file=sys.stderr)
            return {}
    except ImportError as e:
        print(f"[!] {cfg.llm_provider} SDK not installed ({e}). "
              f"Running observe-only: the engine will watch and journal the "
              f"tape but emit no signals.", file=sys.stderr)
        return {}

    def lessons():
        try:
            out = []
            for p in (engine.journal.root / "lessons").glob("*/meta.json"):
                m = json.loads(p.read_text())
                if m.get("status") == "established":
                    out.append(f"{m['tag']} ({m['confidence']:.0%} over "
                               f"{m.get('evidence_count',0)} observations)")
            return sorted(out)[:8]
        except Exception:
            return []

    names = {"A": "Wyckoff Accountant", "B": "Divergence Hunter",
             "C": "VWAP Mean-Reverter"}
    return {sid: LLMStrategist(sid, names[sid], complete,
                               deadline_ms=cfg.llm_deadline_ms,
                               max_retries=cfg.llm_max_retries,
                               cost_per_call_usd=cfg.llm_cost_per_call_usd,
                               lessons_provider=lessons)
            for sid in LENSES}


# ---- commands -------------------------------------------------------------

def cmd_doctor(args) -> int:
    cfg = Config.load(args.config)
    ok, errs = cfg.validate()
    print("=== COLOSSEUM DOCTOR ===\n")
    print("Config:")
    for k, v in sorted(cfg.redacted().items()):
        print(f"  {k:28} {v}")
    print()
    if errs:
        print("Problems:")
        for e in errs:
            print(f"  [!] {e}")
    else:
        print("  [ok] config valid")
    print()

    eng = _engine(cfg)
    chain = eng.ledger.verify_chain()
    print(f"Ledger: chain {'INTACT' if chain.get('ok') else 'BROKEN'} "
          f"({chain.get('records', 0)} records)")
    if eng.ledger.wal.truncated_bytes:
        print(f"  note: {eng.ledger.wal.truncated_bytes} bytes of torn tail were "
              f"discarded on open (normal after a crash; writes are idempotent)")
    print(f"Journal: {eng.index.stats()}")
    cp = eng.registry.last_verified()
    print(f"Checkpoints: {len(eng.registry.all())} total, "
          f"last verified {cp.checkpoint_id if cp else 'NONE'}")
    strat = _build_strategists(cfg, eng)
    print(f"Strategists wired: {sorted(strat) or 'NONE (observe-only mode)'}")
    print(f"Telegram: {'enabled' if cfg.telegram_enabled else 'disabled'}")
    eng.close()
    print("\nVerdict:", "READY" if ok and chain.get("ok") else "NOT READY")
    return 0 if (ok and chain.get("ok")) else 1


def cmd_run(args) -> int:
    cfg = Config.load(args.config)
    ok, errs = cfg.validate()
    if not ok and not args.force:
        for e in errs:
            print(f"[!] {e}", file=sys.stderr)
        print("\nrefusing to start; fix the above or pass --force", file=sys.stderr)
        return 1

    from .feeds.base import FailoverFeed, TickArchiver
    from .delivery.telegram import TelegramPublisher
    from .runner import Runner

    eng = _engine(cfg)
    eng.strategists = _build_strategists(cfg, eng)

    if args.demo:
        from .tests.harness import synthetic_ticks, MockStrategist
        from .feeds.base import IterableFeed
        import datetime as dt
        from .core.clock import dt_to_ns, UTC
        if not eng.strategists:
            eng.strategists = {s: MockStrategist(s, l) for s, l in
                               [("A", "Wyckoff Accountant"),
                                ("B", "Divergence Hunter"),
                                ("C", "VWAP Mean-Reverter")]}
        start = dt_to_ns(dt.datetime(2026, 8, 3, 8, tzinfo=UTC))
        feed = IterableFeed(synthetic_ticks(start, args.ticks), "demo")
    else:
        print("[!] No live broker feed is configured. Implement "
              "feeds.base.WebsocketFeedTemplate for your provider, or run with "
              "--demo to exercise the full pipeline on synthetic tape.",
              file=sys.stderr)
        return 2

    pub = None
    if cfg.telegram_enabled:
        pub = TelegramPublisher(cfg.telegram_token, cfg.telegram_chat_id)
    runner = Runner(cfg, eng, feed, publisher=pub,
                    archiver=TickArchiver(cfg.tick_archive))
    stats = runner.run()
    print(json.dumps(stats.__dict__, indent=2, default=str))
    return 0


def cmd_bootstrap(args) -> int:
    cfg = Config.load(args.config)
    from .replay import bootstrap
    eng = _engine(cfg)
    print(f"Bootstrapping from {args.archive} "
          f"({'all' if not args.days else args.days} days)...")
    rep = bootstrap(args.archive, eng, limit_days=args.days,
                    on_progress=lambda r: print(
                        f"  {r.ticks_in:,} ticks · {r.signals} signals · "
                        f"{r.ticks_per_s:,.0f} ticks/s"))
    print(json.dumps(rep.to_dict(), indent=2))
    eng.close()
    return 0


def cmd_verify_state(args) -> int:
    cfg = Config.load(args.config)
    from .replay import verify_state
    eng = _engine(cfg)
    res = verify_state(eng, args.archive)
    print(json.dumps(res, indent=2))
    eng.close()
    return 0 if res["match"] else 1


def cmd_retro(args) -> int:
    cfg = Config.load(args.config)
    from .journal.writer import Journal
    from .journal.retro import Retrospective
    r = Retrospective(Journal(Path(cfg.root) / "journal"))
    p = r.monthly(args.month) if args.monthly else r.weekly(args.week)
    if p is None:
        print("no sessions found to summarize", file=sys.stderr)
        return 1
    print(p)
    if args.show:
        print("\n" + Path(p).read_text())
    return 0


def cmd_search(args) -> int:
    cfg = Config.load(args.config)
    from .journal.index import JournalIndex
    ix = JournalIndex(Path(cfg.root) / "journal")
    hits = ix.search(args.query, kind=args.kind, limit=args.limit)
    if not hits:
        print("no matches")
        return 0
    for h in hits:
        print(f"\n{h['doc_id']}  [{h['kind']}]  {h.get('session_date','')}")
        print(f"  {h.get('title','')}  resolution={h.get('resolution')} "
              f"R={h.get('realized_r')}")
        print(f"  …{h['snippet']}…")
        print(f"  {h.get('path','')}")
    ix.close()
    return 0


def cmd_scoreboard(args) -> int:
    cfg = Config.load(args.config)
    from .journal.index import JournalIndex
    ix = JournalIndex(Path(cfg.root) / "journal")
    rows = ix.seat_scoreboard()
    if not rows:
        print("no resolved signals yet")
        return 0
    print(f"{'seat':6} {'n':>5} {'wins':>6} {'avg R':>8} {'avg conv':>9} {'mech ok':>8}")
    for r in rows:
        print(f"{str(r['seat_id']):6} {r['n']:>5} {r['wins']:>6} "
              f"{(r['avg_r'] or 0):>8.3f} {(r['avg_conviction'] or 0):>9.2f} "
              f"{r['mechanism_confirmed']:>8}")
    ix.close()
    return 0


def cmd_selftest(args) -> int:
    from .tests.verify import main as verify_main
    return verify_main()


def cmd_simulate(args) -> int:
    """Full pipeline on synthetic tape -- the fastest way to see it work."""
    import datetime as dt
    import shutil
    from .core.clock import UTC, dt_to_ns
    from .tests.harness import MockStrategist, synthetic_ticks
    from .journal.retro import Retrospective

    root = args.root or "/tmp/colosseum_sim"
    shutil.rmtree(root, ignore_errors=True)
    cfg = Config.load(None, root=root, durable_ledger=False)
    eng = _engine(cfg, {s: MockStrategist(s, l) for s, l in
                        [("A", "Wyckoff Accountant"), ("B", "Divergence Hunter"),
                         ("C", "VWAP Mean-Reverter")]})
    start = dt_to_ns(dt.datetime(2026, 8, 3, 8, tzinfo=UTC))
    sessions = set()
    for t in synthetic_ticks(start, args.ticks, seed=args.seed):
        eng.on_tick(t)
        sessions.add(__import__("colosseum.core.clock", fromlist=["session_date"])
                     .session_date(t.t_ns))
    for sd in sorted(sessions):
        eng.close_session(sd, days_elapsed=1.0)
    Retrospective(eng.journal).weekly()

    print(f"\nSimulated {args.ticks:,} ticks over {len(sessions)} session(s)")
    print(f"Signals: {eng._signal_seq} · ledger: {eng.ledger.counts()}")
    print(f"Journal: {eng.index.stats()}")
    print("\nLeague table:")
    for r in eng.colosseum.scoreboard():
        print(f"  {r['rank']}. {r['seat_id']} ({r['lens']}) elo={r['elo']} "
              f"n={r['n']} wr={r['win_rate']:.0%} shelf={r['shelf_weight']:.2f} "
              f"{r['status']}")
    print(f"\nArtifacts under: {root}/journal")
    eng.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="colosseum",
                                description="Gold microstructure signal engine")
    p.add_argument("--config", default="colosseum.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the engine 24/7")
    r.add_argument("--demo", action="store_true",
                   help="run on synthetic tape instead of a live feed")
    r.add_argument("--ticks", type=int, default=200_000)
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("bootstrap", help="cold start from a tick archive")
    b.add_argument("--archive", required=True)
    b.add_argument("--days", type=int, default=None)
    b.set_defaults(fn=cmd_bootstrap)

    v = sub.add_parser("verify-state", help="re-derive state and compare hashes")
    v.add_argument("--archive", required=True)
    v.set_defaults(fn=cmd_verify_state)

    t = sub.add_parser("retro", help="write a retrospective")
    t.add_argument("--weekly", action="store_true")
    t.add_argument("--monthly", action="store_true")
    t.add_argument("--week", default=None)
    t.add_argument("--month", default=None)
    t.add_argument("--show", action="store_true")
    t.set_defaults(fn=cmd_retro)

    s = sub.add_parser("search", help="full-text search the journals")
    s.add_argument("query")
    s.add_argument("--kind", default=None)
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_search)

    sub.add_parser("scoreboard", help="seat performance").set_defaults(fn=cmd_scoreboard)
    sub.add_parser("doctor", help="validate the deployment").set_defaults(fn=cmd_doctor)
    sub.add_parser("selftest", help="run the verification suite").set_defaults(fn=cmd_selftest)

    from .steps import add_parsers as _add_step_parsers
    _add_step_parsers(sub)

    sim = sub.add_parser("simulate", help="full pipeline on synthetic tape")
    sim.add_argument("--ticks", type=int, default=60_000)
    sim.add_argument("--seed", type=int, default=2026)
    sim.add_argument("--root", default=None)
    sim.set_defaults(fn=cmd_simulate)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
