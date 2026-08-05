"""Runnable drivers for START_HERE Steps 3, 4 and 5.

The guide gives these as Python snippets with `...` placeholders for the market
context. These are the finished versions: each is one command, each reports the
diagnostics that decide whether its own output is trustworthy, and each refuses
to print a confident answer it has not earned.

    python -m colosseum.cli verdict   --archive mirror_data/raw/ch.jsonl --bars gold_1m.csv
    python -m colosseum.cli rulebook  --archive mirror_data/raw/ch.jsonl --bars gold_1m.csv
    python -m colosseum.cli forcedflow --bars gold_15m.csv

`verdict` is Step 3 — the one that decides whether any of the rest is worth
doing. `rulebook` is Step 4 and deliberately refuses to run unless Step 3 came
back PROFITABLE, because recovering the rules of a losing channel is how you
spend a month cloning a loss. `forcedflow` is Step 5 and is independent of
Telegram entirely.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .bars import BarLoadError, ContextBuilder, bars_after, load_bars, swept_side
from .core.types import Bar
from .features.forcedflow import ForcedFlowEngine
from .mirror.evaluate import (CostModel, ChannelReport, detect_martingale,
                              evaluate_channel, replay_signal)
from .mirror.parse import MessageParser, ParsedSignal, ParsedUpdate
from .mirror.telegram_ingest import read_archive
from .mirror.provenance import ProvenanceMiner

NS = 1_000_000_000


def _rule(title: str = "") -> None:
    print("\n" + "=" * 66)
    if title:
        print(f"  {title}")
        print("=" * 66)


def _load_bars_or_die(path: str, interval_s: Optional[int],
                      tz: float, symbol: Optional[str]) -> List[Bar]:
    try:
        bars = load_bars(path, interval_s=interval_s, tz_offset_hours=tz,
                         symbol_filter=symbol)
    except BarLoadError as e:
        print(f"could not load bars: {e}", file=sys.stderr)
        raise SystemExit(2)
    span_d = (bars[-1].t_open_ns - bars[0].t_open_ns) / (86_400 * NS)
    print(f"loaded {len(bars):,} bars @ {bars[0].interval_s}s "
          f"spanning {span_d:.0f} days "
          f"({_iso(bars[0].t_open_ns)} -> {_iso(bars[-1].t_open_ns)})")
    return bars


def _iso(t_ns: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t_ns / NS, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _parse_archive(path: str) -> Tuple[List[ParsedSignal], List[ParsedUpdate],
                                       MessageParser, int]:
    parser = MessageParser()
    signals: List[ParsedSignal] = []
    updates: List[ParsedUpdate] = []
    n_msgs = 0
    for rec in read_archive(path):
        n_msgs += 1
        text = rec.get("text") or ""
        if not text:
            continue
        s, u = parser.parse(str(rec["msg_id"]), int(rec["t_ns"]), str(text))
        if s:
            signals.append(s)
        if u:
            updates.append(u)
    return signals, updates, parser, n_msgs


def _parse_health(parser: MessageParser, n_msgs: int, n_sig: int) -> float:
    """Report and return the parse rate.

    START_HERE is emphatic about this and it is the single easiest way to be
    misled: if the parser understood 40% of messages, the verdict describes
    that 40%, not the channel.
    """
    print(f"\nmessages in archive : {n_msgs:,}")
    print(f"signals parsed      : {n_sig:,}")
    print(f"parser stats        : {dict(parser.stats)}")
    considered = sum(v for k, v in parser.stats.items() if k != "ignored")
    rate = (n_sig / considered) if considered else 0.0
    print(f"parse rate          : {rate:.1%} of signal-shaped messages")
    if rate < 0.6 and considered:
        print("\n  ! parse rate is low. The verdict below describes only the "
              "messages\n    that parsed. Inspect the misses before trusting "
              "it — colosseum/mirror/parse.py")
    return rate


def cmd_verdict(args) -> int:
    """STEP 3 — are the channels actually profitable?"""
    bars = _load_bars_or_die(args.bars, args.interval, args.tz, args.symbol)
    cost = CostModel(spread=args.spread, slippage_entry=args.slip_entry,
                     slippage_exit=args.slip_exit, commission=args.commission)

    reports: List[ChannelReport] = []
    for archive in args.archive:
        name = Path(archive).stem
        _rule(f"STEP 3 · {name}")
        signals, updates, parser, n_msgs = _parse_archive(archive)
        if not signals:
            print(f"no signals parsed from {archive} — nothing to evaluate")
            continue
        _parse_health(parser, n_msgs, len(signals))

        # Signals outside the bar coverage cannot be replayed. Silently
        # dropping them would inflate nothing, but it WOULD misreport how much
        # of the channel was actually tested — so it is stated.
        lo, hi = bars[0].t_open_ns, bars[-1].t_open_ns
        in_range = [s for s in signals if lo <= s.t_ns <= hi]
        if len(in_range) < len(signals):
            print(f"\n  ! {len(signals) - len(in_range)} of {len(signals)} "
                  f"signals fall outside the bar coverage and were skipped.\n"
                  f"    Bars cover {_iso(lo)} -> {_iso(hi)}. Widen the bar file "
                  f"to test them.")
        if not in_range:
            print("  no signals overlap the bars — check --tz and the date ranges")
            continue

        outcomes = [replay_signal(s, bars_after(bars, s.t_ns, args.max_bars),
                                  cost, updates) for s in in_range]
        rep = evaluate_channel(name, outcomes, cost)
        reports.append(rep)

        print(f"\n  signals      {rep.n_signals}   filled {rep.n_filled} "
              f"(unfilled {rep.unfilled_rate:.1%})")
        print(f"  win rate     {rep.win_rate:.1%}   breakeven {rep.be_rate:.1%}")
        print(f"  expectancy   {rep.expectancy_r:+.3f}R   total {rep.total_r:+.1f}R")
        print(f"  profit factor{rep.profit_factor:>7.2f}   sharpe {rep.sharpe:.2f}")
        print(f"  max drawdown {rep.max_dd_r:.1f}R   mean MAE {rep.mean_mae_r:.2f}R")
        print(f"  tp hits      {rep.tp_hit_distribution}")
        print(f"\n  VERDICT: {rep.verdict}")
        for w in rep.warnings:
            print(f"    warning  {w}")

        mart = detect_martingale(in_range)
        print(f"\n  martingale: {mart.get('assessment')}")

        if args.json:
            out = Path(args.json_dir or ".") / f"verdict_{name}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(
                {"report": rep.to_dict(), "martingale": mart,
                 "cost": {"spread": cost.spread, "round_trip": cost.round_trip}},
                indent=2, default=str))
            print(f"\n  wrote {out}")

    if reports:
        _rule("WHAT TO DO NEXT")
        for rep in reports:
            v = rep.verdict.upper()
            if "NOT PROFITABLE" in v or "UNPROFITABLE" in v:
                tag, nxt = "NOT PROFITABLE", ("STOP. Study it as a negative example — "
                                              "that is a result, not a failure.")
            elif "MARGINAL" in v:
                tag, nxt = "MARGINAL", ("Collect more data. Do NOT talk yourself "
                                        "into this one.")
            elif "PROFITABLE" in v:
                tag, nxt = "PROFITABLE", ("Proceed to Step 4:  python -m "
                                          "colosseum.cli rulebook ...")
            else:
                tag, nxt = "INCONCLUSIVE", "Read the warnings above."
            print(f"  {rep.name:<20} {tag:<15} {nxt}")
    return 0


def cmd_rulebook(args) -> int:
    """STEP 4 — recover the rulebook. Only meaningful if Step 3 said PROFITABLE."""
    bars = _load_bars_or_die(args.bars, args.interval, args.tz, args.symbol)

    for archive in args.archive:
        name = Path(archive).stem
        _rule(f"STEP 4 · {name}")

        if not args.force:
            vfile = Path(args.json_dir or ".") / f"verdict_{name}.json"
            if not vfile.exists():
                print(f"no {vfile} found. Run Step 3 first:\n"
                      f"  python -m colosseum.cli verdict --archive {archive} "
                      f"--bars {args.bars} --json\n"
                      f"Or pass --force to mine anyway (you are then mining the "
                      f"rules of an unverified channel).")
                continue
            verdict = json.loads(vfile.read_text())["report"]["verdict"].upper()
            if "PROFITABLE" not in verdict or "NOT PROFITABLE" in verdict:
                print(f"Step 3 verdict was: {verdict}\n"
                      f"Refusing to mine a rulebook for a channel that did not "
                      f"pass.\nThis is the guard START_HERE asks for. Override "
                      f"with --force if you want it as a negative example.")
                continue

        signals, _updates, parser, n_msgs = _parse_archive(archive)
        if not signals:
            print("no signals parsed — nothing to mine")
            continue
        _parse_health(parser, n_msgs, len(signals))

        ctxb = ContextBuilder(bars, atr_period=args.atr,
                              swing_lookback=args.swing)
        miner = ProvenanceMiner(tolerance_atr=args.tolerance)
        n_ctx = 0
        for s in signals:
            ctx = ctxb.at(s.t_ns)
            if ctx is None:
                continue
            n_ctx += 1
            if s.stop:
                miner.observe("stop", s.stop, ctx)
            if s.entry:
                miner.observe("entry", s.entry, ctx)
            for i, tp in enumerate(s.targets, 1):
                miner.observe(f"tp{i}", tp, ctx)
                if s.risk and s.entry is not None:
                    miner.observe_r_multiple(f"tp{i}", abs(tp - s.entry) / s.risk)

        print(f"\nsignals with usable market context: {n_ctx} of {len(signals)}")
        if n_ctx < 20:
            print("  ! fewer than 20 contexts. Any 'rule' found here is noise.")

        rep = miner.report(min_support=args.min_support)

        # Roles the channel actually posted, so a role that was observed but
        # explained by nothing is reported as such rather than silently
        # vanishing from the output. "We looked and found no anchor" and "we
        # never looked" are very different claims.
        observed: List[str] = []
        for s in signals:
            for role, present in (("entry", s.entry), ("stop", s.stop)):
                if present and role not in observed:
                    observed.append(role)
            for i in range(1, len(s.targets) + 1):
                if f"tp{i}" not in observed:
                    observed.append(f"tp{i}")

        by_role = rep.get("rules_by_role", {})
        print("\n  LEVEL PROVENANCE")
        for role in observed:
            rules = by_role.get(role) or []
            if rules:
                print(f"    {role:<8} -> {rules[0]['rule']}")
                for alt in rules[1:3]:
                    print(f"    {'':<8}    (also: {alt['rule']})")
            else:
                print(f"    {role:<8} -> no anchor explains this level "
                      f"(observed, nothing matched)")

        rmr = rep.get("r_multiple_rules", [])
        if rmr:
            print("\n  R-MULTIPLE STRUCTURE")
            for r in rmr:
                print(f"    {r.get('interpretation')}")

        amb = rep.get("ambiguous_attributions", [])
        if amb:
            print(f"\n  AMBIGUOUS — {len(amb)} level(s) explained equally well by "
                  f"two anchors.\n  Do not pick one and call it the rule:")
            for a in amb[:8]:
                print(f"    {a}")

        if args.json:
            out = Path(args.json_dir or ".") / f"rulebook_{name}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(rep, indent=2, default=str))
            print(f"\n  wrote {out}")
    return 0


def cmd_forcedflow(args) -> int:
    """STEP 5 — locate real forced-flow windows; reject the folklore."""
    bars = _load_bars_or_die(args.bars, args.interval, args.tz, args.symbol)

    span_d = (bars[-1].t_open_ns - bars[0].t_open_ns) / (86_400 * NS)
    if span_d < 365:
        print(f"\n  ! only {span_d:.0f} days of history. START_HERE asks for "
              f"1-2 years minimum.\n    The stability test splits the sample in "
              f"half; with this little data each\n    half is too thin to reject "
              f"anything, which is when folklore survives.")

    _rule("STEP 5 · FORCED FLOW")
    ff = ForcedFlowEngine()
    prev: Optional[Bar] = None
    for b in bars:
        ff.update(b, swept=swept_side(prev, b) if prev else None)
        prev = b

    rep = ff.report(fdr=args.fdr)

    windows = rep.get("windows", [])
    tradeable = [w for w in windows if w.get("tradeable")]
    print(f"\n  {len(tradeable)} of {len(windows)} candidate windows survived "
          f"FDR correction + sign stability\n")
    for w in windows:
        mark = "TRADEABLE" if w.get("tradeable") else " rejected"
        print(f"  [{mark}] {w.get('window'):<20} "
              f"vol {w.get('vol_ratio', 0):.2f}x  vlm {w.get('volume_ratio', 0):.2f}x  "
              f"n={w.get('n_bars', 0):,}  stab {w.get('stability', 0):.2f}  "
              f"p={w.get('p_value', 1):.4f}")
        for f in w.get("findings", []):
            print(f"              {f}")

    mult = rep.get("volatility_multipliers", {})
    if mult:
        print("\n  VOLATILITY MULTIPLIERS — the immediate payoff.")
        print("  Feed these into stop and target sizing now. A stop sized for")
        print("  the London/NY overlap is far too tight for Asia.\n")
        for k, v in sorted(mult.items(), key=lambda kv: -kv[1]):
            print(f"    {k:<24} x{v:.3f}")

    if args.json:
        out = Path(args.json_dir or ".") / "forcedflow.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=2, default=str))
        print(f"\n  wrote {out}")
    return 0


def add_parsers(sub) -> None:
    """Wire Steps 3-5 into the main CLI."""

    def _bar_args(p) -> None:
        p.add_argument("--bars", required=True,
                       help="CSV/Parquet of OHLCV bars (time,open,high,low,close,volume)")
        p.add_argument("--interval", type=int, default=None,
                       help="bar interval in seconds (inferred if omitted)")
        p.add_argument("--tz", type=float, default=0.0,
                       help="UTC offset the bar timestamps are already in, e.g. 2 "
                            "for a UTC+2 broker export. Getting this wrong moves "
                            "every signal into the wrong session.")
        p.add_argument("--symbol", default=None,
                       help="filter rows by a symbol/ticker column")
        p.add_argument("--json", action="store_true", help="also write JSON output")
        p.add_argument("--json-dir", default="mirror_data/reports")

    v = sub.add_parser("verdict",
                       help="STEP 3: replay a channel against your own bars")
    v.add_argument("--archive", required=True, nargs="+",
                   help="one or more mirror_data/raw/<channel>.jsonl files")
    _bar_args(v)
    v.add_argument("--spread", type=float, default=0.30)
    v.add_argument("--slip-entry", type=float, default=0.10)
    v.add_argument("--slip-exit", type=float, default=0.15)
    v.add_argument("--commission", type=float, default=0.10)
    v.add_argument("--max-bars", type=int, default=5000,
                   help="max bars to walk forward per signal")
    v.set_defaults(fn=cmd_verdict)

    r = sub.add_parser("rulebook",
                       help="STEP 4: recover level provenance (needs a PROFITABLE verdict)")
    r.add_argument("--archive", required=True, nargs="+")
    _bar_args(r)
    r.add_argument("--tolerance", type=float, default=1.2,
                   help="anchor match tolerance in ATR")
    r.add_argument("--atr", type=int, default=14)
    r.add_argument("--swing", type=int, default=3,
                   help="bars either side required to confirm a swing")
    r.add_argument("--min-support", type=int, default=5)
    r.add_argument("--force", action="store_true",
                   help="mine even without a PROFITABLE Step 3 verdict")
    r.set_defaults(fn=cmd_rulebook)

    f = sub.add_parser("forcedflow",
                       help="STEP 5: forced-flow windows + volatility multipliers")
    _bar_args(f)
    f.add_argument("--fdr", type=float, default=0.10)
    f.set_defaults(fn=cmd_forcedflow)
