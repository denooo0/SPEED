"""Chart image → vision analysis → discipline gate → Telegram limit order.

The full flow the operator asked for: an LLM analyzes a chart and sends a
limit order to Telegram. Discipline is enforced between analysis and send so
low-quality or doctrine-violating setups never reach the phone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from src.copilot import discipline_gate as gate
from src.copilot.schema import SetupStats, TradeProposal
from src.vision.chart_analyst import ChartAnalyst
from src.vision.schema import ChartAnalysis

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    analysis: ChartAnalysis
    sent: bool
    decision: str                 # "SENT" | "BLOCKED" | "NO_SETUP"
    reasons: List[str]
    message: str                  # the formatted TG message (whether sent or not)


def analysis_to_proposal(a: ChartAnalysis) -> Optional[TradeProposal]:
    """Convert a vision analysis into a TradeProposal for the discipline gate."""
    if not a.setup_present or a.direction not in ("long", "short"):
        return None
    if a.entry_limit is None or a.stop is None or not a.targets:
        return None
    return TradeProposal(
        instrument=a.instrument,
        direction=a.direction,
        entry=a.entry_limit,
        stop=a.stop,
        target=a.targets[0],
        setup=f"vision:{a.regime}",
        thesis=a.thesis,
        kill_thesis=a.kill_thesis,
        conviction=max(1, min(5, round(a.confidence * 5))),
        session="unknown",
        notes=a.notes,
    )


def format_limit_order(a: ChartAnalysis) -> str:
    """A phone-friendly limit-order message."""
    if not a.setup_present or a.direction == "none":
        return (
            f"*ATLAS chart read — {a.instrument} {a.timeframe}*\n"
            f"Regime: {a.regime}\n"
            f"No high-quality setup. Default is NO. Sitting on hands.\n"
            f"{a.notes}".strip()
        )
    tps = " / ".join(f"{t:g}" for t in a.targets) if a.targets else "—"
    lines = [
        f"*ATLAS LIMIT ORDER — {a.instrument} {a.timeframe}*",
        f"{a.direction.upper()} @ limit `{a.entry_limit:g}`",
        f"SL `{a.stop:g}`   TP `{tps}`   (R:R {a.rr:.2f})",
        f"Regime: {a.regime}   Conf: {a.confidence:.2f}",
    ]
    if a.trapped_party:
        lines.append(f"Trapped: {a.trapped_party}")
    lines.append(f"Why: {a.thesis}")
    lines.append(f"Kill: {a.kill_thesis}")
    lines.append("\n_Advisory. You place the order._")
    return "\n".join(lines)


class ChartToTelegram:
    def __init__(
        self,
        analyst: ChartAnalyst,
        telegram: Any,                       # TelegramBot (or any .send_message)
        gate_config: Optional[gate.GateConfig] = None,
        setup_stats_lookup: Optional[Any] = None,  # callable(setup)->SetupStats
    ) -> None:
        self.analyst = analyst
        self.telegram = telegram
        self.gate_config = gate_config or gate.GateConfig()
        self.setup_stats_lookup = setup_stats_lookup

    def run(
        self,
        image_path: str | Path,
        instrument_hint: str = "XAUUSD",
        extra_context: str = "",
        in_news_window: bool = False,
    ) -> PipelineResult:
        analysis = self.analyst.analyze_image(image_path, instrument_hint, extra_context)
        msg = format_limit_order(analysis)

        proposal = analysis_to_proposal(analysis)
        if proposal is None:
            # No setup — optionally notify, but do not send an order.
            return PipelineResult(analysis, sent=False, decision="NO_SETUP",
                                  reasons=["no high-quality setup"], message=msg)

        stats = (self.setup_stats_lookup(proposal.setup)
                 if self.setup_stats_lookup else SetupStats(setup=proposal.setup))
        checks = gate.evaluate(
            proposal, stats, open_positions=0,
            in_news_window=in_news_window, config=self.gate_config,
        )
        decision = gate.decide(checks)
        blockers = [c.message for c in checks if c.severity == "block" and not c.passed]

        if decision == "BLOCK":
            return PipelineResult(analysis, sent=False, decision="BLOCKED",
                                  reasons=blockers, message=msg)

        # Approved (possibly with warnings) — send the limit order.
        warn = [c.message for c in checks if c.severity == "warn" and not c.passed]
        if warn:
            msg = msg + "\n\n⚠️ " + " | ".join(warn)
        sent = bool(self.telegram.send_message(msg))
        return PipelineResult(analysis, sent=sent, decision="SENT",
                              reasons=warn, message=msg)
