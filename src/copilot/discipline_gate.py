"""Pre-trade discipline gate for discretionary trades.

Enforces the ATLAS doctrine on the OPERATOR's own intended trades — the same
laws that governed the (abandoned) signal generator, now as a coach on human
decisions. Blocks the reckless, warns on the marginal, and surfaces the
operator's own history with the setup so they trade with their evidence in
front of them.

Deterministic and testable. No LLM required (an LLM coaching layer can enrich
the messages later, but the gate itself is rules).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from src.copilot.schema import (
    DisciplineCheck,
    SetupStats,
    TradeProposal,
)

# Vague kill-thesis blacklist — reused idea from the signal-generator doctrine.
_VAGUE_KILL = re.compile(
    r"\b(if\s+it\s+goes?\s+against\s+me|if\s+i'?m\s+wrong|gut\s+feel|vibes?|"
    r"if\s+the\s+trend\s+reverses)\b",
    re.IGNORECASE,
)


@dataclass
class GateConfig:
    min_rr: float = 2.0                 # Law 4 (relaxed to 2.0 for discretionary)
    min_kill_len: int = 25              # Law 3 — kill thesis must be specific
    max_open_positions: int = 1         # Law 11 — one bullet, one chamber
    daily_loss_cap_pct: float = 4.0     # halt new trades past this daily loss
    max_account_dd_halt_pct: float = 20.0
    losing_setup_warn_n: int = 8        # warn if setup has >= this many trades and loses
    news_block: bool = True             # block inside a news window (Law 12)


def evaluate(
    proposal: TradeProposal,
    setup_stats: SetupStats,
    open_positions: int = 0,
    day_pnl_pct: float = 0.0,
    account_dd_pct: float = 0.0,
    in_news_window: bool = False,
    config: Optional[GateConfig] = None,
) -> List[DisciplineCheck]:
    """Return the list of discipline checks for this proposed trade."""
    cfg = config or GateConfig()
    checks: List[DisciplineCheck] = []

    # --- Law 11: one position at a time ---
    checks.append(DisciplineCheck(
        name="one_position",
        passed=open_positions < cfg.max_open_positions,
        severity="block",
        message=(
            f"{open_positions} position(s) already open (max {cfg.max_open_positions}). "
            "One bullet, one chamber." if open_positions >= cfg.max_open_positions
            else "No conflicting open position."
        ),
    ))

    # --- Account drawdown halt ---
    checks.append(DisciplineCheck(
        name="drawdown_halt",
        passed=account_dd_pct < cfg.max_account_dd_halt_pct,
        severity="block",
        message=(
            f"Account drawdown {account_dd_pct:.1f}% ≥ {cfg.max_account_dd_halt_pct:.0f}% halt. "
            "Stop. Re-derive the thesis before trading." if account_dd_pct >= cfg.max_account_dd_halt_pct
            else f"Drawdown {account_dd_pct:.1f}% within limit."
        ),
    ))

    # --- Daily loss cap ---
    checks.append(DisciplineCheck(
        name="daily_loss_cap",
        passed=day_pnl_pct > -cfg.daily_loss_cap_pct,
        severity="block",
        message=(
            f"Down {day_pnl_pct:.1f}% today (cap {cfg.daily_loss_cap_pct:.0f}%). "
            "Done for the day — no revenge trades." if day_pnl_pct <= -cfg.daily_loss_cap_pct
            else f"Day P&L {day_pnl_pct:+.1f}% within cap."
        ),
    ))

    # --- News window (Law 12) ---
    if cfg.news_block:
        checks.append(DisciplineCheck(
            name="news_window",
            passed=not in_news_window,
            severity="block",
            message=("Inside a tier-1 news window. Wait 15 min — the tape is lying."
                     if in_news_window else "Not in a news window."),
        ))

    # --- Law 4: R:R ---
    rr = proposal.rr
    checks.append(DisciplineCheck(
        name="risk_reward",
        passed=rr >= cfg.min_rr,
        severity="block",
        message=(f"R:R {rr:.2f} < {cfg.min_rr:.1f} minimum. The asymmetry is the edge."
                 if rr < cfg.min_rr else f"R:R {rr:.2f} ✓"),
    ))

    # --- Law 3: specific kill thesis ---
    kt = (proposal.kill_thesis or "").strip()
    kt_ok = len(kt) >= cfg.min_kill_len and not _VAGUE_KILL.search(kt)
    checks.append(DisciplineCheck(
        name="kill_thesis",
        passed=kt_ok,
        severity="block",
        message=("Kill thesis is vague or too short. Name the single observable "
                 "that voids the trade." if not kt_ok else "Kill thesis is specific ✓"),
    ))

    # --- Law 2: a real thesis ---
    checks.append(DisciplineCheck(
        name="thesis_present",
        passed=len((proposal.thesis or "").strip()) >= 20,
        severity="warn",
        message=("Thesis is thin. Who is trapped, and why does this resolve your way?"
                 if len((proposal.thesis or "").strip()) < 20 else "Thesis present ✓"),
    ))

    # --- Stop is on the correct side ---
    stop_ok = (proposal.direction == "long" and proposal.stop < proposal.entry) or \
              (proposal.direction == "short" and proposal.stop > proposal.entry)
    checks.append(DisciplineCheck(
        name="stop_side",
        passed=stop_ok,
        severity="block",
        message=("Stop is on the wrong side of entry for a "
                 f"{proposal.direction}." if not stop_ok else "Stop placement ✓"),
    ))

    # --- Learning: this setup's own track record ---
    if setup_stats.n >= cfg.losing_setup_warn_n and setup_stats.expectancy_r < 0:
        checks.append(DisciplineCheck(
            name="setup_track_record",
            passed=False,
            severity="warn",
            message=(
                f"'{setup_stats.setup}' is {setup_stats.wins}-{setup_stats.losses} "
                f"({setup_stats.win_rate:.0%}, {setup_stats.expectancy_r:+.2f}R) over "
                f"{setup_stats.n} of your trades — a net loser so far. Are you sure?"
            ),
        ))
    elif setup_stats.n > 0:
        checks.append(DisciplineCheck(
            name="setup_track_record",
            passed=True,
            severity="info",
            message=(
                f"'{setup_stats.setup}' history: {setup_stats.wins}-{setup_stats.losses} "
                f"({setup_stats.win_rate:.0%}, {setup_stats.expectancy_r:+.2f}R) over "
                f"{setup_stats.n} trades."
            ),
        ))

    return checks


def decide(checks: List[DisciplineCheck]) -> str:
    blocked = any(c.severity == "block" and not c.passed for c in checks)
    warned = any(c.severity == "warn" and not c.passed for c in checks)
    if blocked:
        return "BLOCK"
    if warned:
        return "APPROVE_WITH_WARNINGS"
    return "APPROVE"
