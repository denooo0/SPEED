"""TradeCopilot — the orchestrator for discretionary-trading assistance.

Ties together the discipline gate, the position sizer (fractional Kelly on the
operator's own per-setup history), and the journal. The operator proposes a
trade; the copilot coaches, sizes, and (if they proceed) logs it. On close it
updates the learning substrate. It gets better as the journal fills.
"""
from __future__ import annotations

from typing import List, Optional

from src.copilot import discipline_gate as gate
from src.copilot.journal import TradeJournal
from src.copilot.position_sizer import (
    SizerConfig,
    recommend_risk_pct,
    size_from_risk,
)
from src.copilot.schema import CopilotVerdict, TradeProposal, TradeRecord


class TradeCopilot:
    def __init__(
        self,
        journal: Optional[TradeJournal] = None,
        gate_config: Optional[gate.GateConfig] = None,
        sizer_config: Optional[SizerConfig] = None,
    ) -> None:
        self.journal = journal or TradeJournal()
        self.gate_config = gate_config or gate.GateConfig()
        self.sizer_config = sizer_config or SizerConfig()

    def review(
        self,
        proposal: TradeProposal,
        account_equity: float,
        day_pnl_pct: float = 0.0,
        account_dd_pct: float = 0.0,
        in_news_window: bool = False,
    ) -> CopilotVerdict:
        """Coach + size a proposed trade. Does NOT log it — call `commit`."""
        stats = self.journal.stats_for(proposal.setup)
        open_positions = len(self.journal.open_positions())

        checks = gate.evaluate(
            proposal=proposal,
            setup_stats=stats,
            open_positions=open_positions,
            day_pnl_pct=day_pnl_pct,
            account_dd_pct=account_dd_pct,
            in_news_window=in_news_window,
            config=self.gate_config,
        )
        decision = gate.decide(checks)

        risk_pct, rationale = recommend_risk_pct(
            stats, conviction=proposal.conviction, config=self.sizer_config
        )
        # If blocked, size is moot — report 0.
        size = 0.0
        if decision != "BLOCK":
            size = size_from_risk(risk_pct, account_equity, proposal.risk_per_unit)

        coaching = self._coach(proposal, stats, decision, checks)

        return CopilotVerdict(
            proposal=proposal,
            decision=decision,
            checks=checks,
            recommended_risk_pct=risk_pct,
            recommended_size=round(size, 6),
            sizing_rationale=rationale,
            coaching=coaching,
        )

    def commit(self, verdict: CopilotVerdict) -> Optional[TradeRecord]:
        """Log an approved trade to the journal. Refuses a BLOCK."""
        if verdict.decision == "BLOCK":
            return None
        return self.journal.open_trade(
            proposal=verdict.proposal,
            risk_pct=verdict.recommended_risk_pct,
            size=verdict.recommended_size,
        )

    def close(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str = "manual",
        notes: str = "",
    ) -> Optional[TradeRecord]:
        return self.journal.close_trade(trade_id, exit_price, exit_reason, notes)

    # -- coaching --------------------------------------------------------
    @staticmethod
    def _coach(
        proposal: TradeProposal,
        stats,
        decision: str,
        checks: List[gate.DisciplineCheck],
    ) -> List[str]:
        out: List[str] = []
        if decision == "BLOCK":
            for c in checks:
                if c.severity == "block" and not c.passed:
                    out.append(f"BLOCKED — {c.message}")
            return out
        if decision == "APPROVE_WITH_WARNINGS":
            for c in checks:
                if c.severity == "warn" and not c.passed:
                    out.append(f"WARNING — {c.message}")
        # Positive reinforcement of the discipline that passed.
        if proposal.rr >= 3:
            out.append(f"Strong asymmetry ({proposal.rr:.1f}R) — this is the kind of trade to size into.")
        if stats.n >= 20 and stats.expectancy_r > 0:
            out.append(
                f"'{proposal.setup}' is a proven edge for you "
                f"({stats.expectancy_r:+.2f}R over {stats.n} trades). Trust it."
            )
        return out
