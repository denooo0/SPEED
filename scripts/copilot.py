#!/usr/bin/env python3
"""ATLAS Trade Copilot — CLI for discretionary-trading assistance.

The operator proposes a trade; the copilot enforces discipline, sizes it from
YOUR own historical edge, and journals it. On close it learns. It gets better
the more you log.

Examples
--------
Review + log a trade:
    python scripts/copilot.py review \\
        --instrument XAUUSD --direction short \\
        --entry 4128.21 --stop 4140 --target 4090 \\
        --setup htf-retracement-short \\
        --thesis "H1 downtrend, sold retracement into declining MA; late longs trapped" \\
        --kill "H1 closes back above 4140 with positive momentum" \\
        --conviction 4 --session ny --equity 100000 --commit

Close a trade:
    python scripts/copilot.py close --id <trade_id> --exit 4030 --reason TP

Show your evolving performance profile:
    python scripts/copilot.py profile
    python scripts/copilot.py open
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.copilot.copilot import TradeCopilot
from src.copilot.schema import TradeProposal


def _print_verdict(v) -> None:
    print("=" * 66)
    print(f"COPILOT VERDICT: {v.decision}")
    print("=" * 66)
    p = v.proposal
    print(f"{p.direction.upper()} {p.instrument}  entry {p.entry}  stop {p.stop}  "
          f"target {p.target}  (R:R {p.rr:.2f})")
    print()
    for c in v.checks:
        mark = "OK " if c.passed else ("XX " if c.severity == "block" else "!! ")
        if c.passed and c.severity == "info":
            mark = "-- "
        print(f"  {mark}[{c.severity:<5}] {c.name}: {c.message}")
    print()
    print(f"Recommended risk: {v.recommended_risk_pct:.2f}%   size: {v.recommended_size:g} units")
    print(f"  {v.sizing_rationale}")
    if v.coaching:
        print("\nCoaching:")
        for line in v.coaching:
            print(f"  • {line}")
    print("=" * 66)


def cmd_review(args) -> int:
    cop = TradeCopilot()
    proposal = TradeProposal(
        instrument=args.instrument,
        direction=args.direction,
        entry=args.entry,
        stop=args.stop,
        target=args.target,
        setup=args.setup,
        thesis=args.thesis,
        kill_thesis=args.kill,
        conviction=args.conviction,
        session=args.session,
        notes=args.notes or "",
    )
    verdict = cop.review(
        proposal,
        account_equity=args.equity,
        day_pnl_pct=args.day_pnl,
        account_dd_pct=args.drawdown,
        in_news_window=args.news,
    )
    _print_verdict(verdict)
    if args.commit:
        if verdict.decision == "BLOCK":
            print("\nNOT logged — trade is BLOCKED. Fix the blockers or skip.")
            return 1
        rec = cop.commit(verdict)
        print(f"\nLogged as {rec.trade_id} (status OPEN).")
    else:
        print("\n(dry run — pass --commit to log this trade.)")
    return 0


def cmd_close(args) -> int:
    cop = TradeCopilot()
    rec = cop.close(args.id, exit_price=args.exit, exit_reason=args.reason, notes=args.notes or "")
    if rec is None:
        print(f"No OPEN trade with id {args.id}.")
        return 1
    print(f"Closed {rec.trade_id}: exit {rec.exit_price}  R={rec.r_multiple:+.2f}  "
          f"acct P&L {rec.pnl_account*100:+.2f}%  reason {rec.exit_reason}")
    return 0


def cmd_profile(args) -> int:
    cop = TradeCopilot()
    stats = cop.journal.setup_stats()
    if not stats:
        print("No closed trades yet. Log some with `review --commit` then `close`.")
        return 0
    print("=" * 66)
    print("YOUR PERFORMANCE PROFILE (per setup)")
    print("=" * 66)
    print(f"{'setup':<28}{'n':>4}{'win%':>7}{'exp(R)':>9}{'avgW':>7}{'avgL':>7}")
    for s in sorted(stats.values(), key=lambda x: -x.expectancy_r):
        print(f"{s.setup:<28}{s.n:>4}{s.win_rate*100:>6.0f}%{s.expectancy_r:>+9.2f}"
              f"{s.avg_win_r:>+7.2f}{s.avg_loss_r:>+7.2f}")
    print("=" * 66)
    print("Setups above 0 exp(R) are your real edges — size into them.")
    print("Setups below 0 over many trades — stop taking them.")
    return 0


def cmd_open(args) -> int:
    cop = TradeCopilot()
    opens = cop.journal.open_positions()
    if not opens:
        print("No open positions.")
        return 0
    for r in opens:
        print(f"{r.trade_id}  {r.direction} {r.instrument}  entry {r.entry} "
              f"stop {r.stop} target {r.target}  risk {r.risk_pct:.2f}%  [{r.setup}]")
    return 0


def main(argv) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="review + optionally log a proposed trade")
    r.add_argument("--instrument", required=True)
    r.add_argument("--direction", required=True, choices=["long", "short"])
    r.add_argument("--entry", type=float, required=True)
    r.add_argument("--stop", type=float, required=True)
    r.add_argument("--target", type=float, required=True)
    r.add_argument("--setup", required=True)
    r.add_argument("--thesis", default="")
    r.add_argument("--kill", default="", help="kill thesis — the single observable that voids it")
    r.add_argument("--conviction", type=int, default=3)
    r.add_argument("--session", default="unknown")
    r.add_argument("--equity", type=float, default=100000.0)
    r.add_argument("--day-pnl", type=float, default=0.0, help="today's P&L %% so far")
    r.add_argument("--drawdown", type=float, default=0.0, help="current account drawdown %%")
    r.add_argument("--news", action="store_true", help="inside a tier-1 news window")
    r.add_argument("--notes", default="")
    r.add_argument("--commit", action="store_true", help="log the trade if not blocked")
    r.set_defaults(func=cmd_review)

    c = sub.add_parser("close", help="close an open trade")
    c.add_argument("--id", required=True)
    c.add_argument("--exit", type=float, required=True)
    c.add_argument("--reason", default="manual")
    c.add_argument("--notes", default="")
    c.set_defaults(func=cmd_close)

    pr = sub.add_parser("profile", help="show per-setup performance (the learning)")
    pr.set_defaults(func=cmd_profile)

    o = sub.add_parser("open", help="list open positions")
    o.set_defaults(func=cmd_open)

    args = p.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
