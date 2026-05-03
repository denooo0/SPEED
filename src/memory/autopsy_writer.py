"""Closed position -> markdown autopsy.

Deterministic skeleton for Phase 0: frontmatter from trade events, body from
the triggering SITUATION REPORT (if available). LLM-generated post-mortem
prose can be layered on later by re-reading + appending to the same file.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.llm.schema import SituationReport
from src.memory.markdown_store import AutopsyRecord, MarkdownMemory

logger = logging.getLogger(__name__)


class AutopsyWriter:
    """Render closed-position events into a markdown autopsy."""

    def __init__(self, memory: MarkdownMemory) -> None:
        self.memory = memory

    def from_closed_position(
        self,
        position: Dict[str, Any],
        trade_events: List[Dict[str, Any]],
        triggering_sr: Optional[SituationReport] = None,
        instrument: str = "XAUUSD",
    ) -> Path:
        autopsy_id = self._build_id(position, instrument)
        result, r_multiple = self._classify_outcome(position, trade_events)
        kill_triggered = self._kill_thesis_triggered(trade_events)
        tags = self._tags(
            result,
            triggering_sr,
            kill_triggered,
            direction=position.get("direction"),
        )

        body = self._render_body(
            position=position,
            trade_events=trade_events,
            sr=triggering_sr,
            result=result,
            r_multiple=r_multiple,
        )
        record = AutopsyRecord(
            autopsy_id=autopsy_id,
            instrument=instrument,
            setup=triggering_sr.regime if triggering_sr else None,
            regime=triggering_sr.regime if triggering_sr else None,
            result=result,
            r_multiple=r_multiple,
            kill_thesis_triggered=kill_triggered,
            tags=tags,
            body=body,
        )
        path = self.memory.write_autopsy(record)
        logger.info("autopsy written: %s (result=%s r=%.2f)", path, result, r_multiple)
        return path

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _build_id(position: Dict[str, Any], instrument: str) -> str:
        position_id = position.get("position_id") or ""
        if not position_id:
            raise ValueError("autopsy requires a non-empty position_id")
        ts_ms = int(position.get("entry_time", 0))
        dt = (
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            if ts_ms
            else datetime.now(tz=timezone.utc)
        )
        # Seconds resolution + 12 chars of UUID hex makes intra-second collisions
        # statistically unreachable.
        return (
            f"{dt.strftime('%Y-%m-%d-%H%M%S')}-{instrument.lower()}-{position_id[:12]}"
        )

    @staticmethod
    def _classify_outcome(
        position: Dict[str, Any],
        trade_events: List[Dict[str, Any]],
    ) -> tuple[str, float]:
        """Return (result_label, r_multiple). Direction-agnostic."""
        entry = float(position.get("entry_price") or 0)
        sl = float(position.get("sl") or 0)
        # Risk per unit is always |entry - sl|; longs and shorts both have positive risk.
        risk_per_unit = abs(entry - sl)
        if risk_per_unit == 0:
            return ("breakeven", 0.0)
        pnl_total = sum(float(e.get("pnl") or 0) for e in trade_events if e.get("pnl") is not None)
        size = float(position.get("entry_size") or 1)
        avg_pnl_per_unit = pnl_total / size if size else 0.0
        r = avg_pnl_per_unit / risk_per_unit

        if pnl_total > 0:
            return ("won", r)
        if pnl_total < 0:
            return ("lost", r)
        return ("breakeven", 0.0)

    @staticmethod
    def _kill_thesis_triggered(trade_events: List[Dict[str, Any]]) -> bool:
        return any(e.get("rule_matched") == "SL" for e in trade_events)

    @staticmethod
    def _tags(
        result: str,
        sr: Optional[SituationReport],
        kill_triggered: bool,
        direction: Optional[str] = None,
    ) -> List[str]:
        tags = [result]
        if kill_triggered:
            tags.append("kill-thesis-clean")
        if sr is not None:
            tags.append(f"regime:{sr.regime}")
            tags.append(f"direction:{sr.trade_proposal.direction}")
        elif direction:
            tags.append(f"direction:{direction}")
        return tags

    @staticmethod
    def _render_body(
        position: Dict[str, Any],
        trade_events: List[Dict[str, Any]],
        sr: Optional[SituationReport],
        result: str,
        r_multiple: float,
    ) -> str:
        lines: List[str] = []
        lines.append(f"## Outcome\n\n- Result: **{result}**")
        lines.append(f"- R-multiple: **{r_multiple:+.2f}**")
        lines.append(f"- Entry: `{position.get('entry_price')}`  SL: `{position.get('sl')}`")
        lines.append(
            f"- TP1/TP2/TP3: `{position.get('tp1')}` / `{position.get('tp2')}` / `{position.get('tp3')}`"
        )
        lines.append("")

        lines.append("## Trade events")
        if not trade_events:
            lines.append("- (none recorded)")
        for e in trade_events:
            evt = e.get("event")
            price = e.get("price")
            size = e.get("size")
            pnl = e.get("pnl")
            rule = e.get("rule_matched")
            pnl_str = f"  P&L: {pnl:+.2f}" if pnl is not None else ""
            rule_str = f"  ({rule})" if rule else ""
            lines.append(f"- `{evt}` @ `{price}` size `{size}`{pnl_str}{rule_str}")
        lines.append("")

        if sr is not None:
            lines.append("## What the SITUATION REPORT predicted")
            lines.append(f"- Regime: `{sr.regime}`")
            lines.append(f"- Confidence: `{sr.confidence:.2f}`")
            lines.append(f"- Thesis: {sr.thesis}")
            lines.append(f"- Kill thesis: {sr.kill_thesis}")
            lines.append("")

        lines.append("## What actually happened")
        lines.append("(TODO — fill with post-mortem prose. The brain can rewrite this section in a")
        lines.append("follow-up cycle once it's seen the events vs. the prediction.)")
        lines.append("")

        lines.append("## One-line lesson")
        lines.append("(TODO — single sentence the next SITUATION REPORT should know.)")

        return "\n".join(lines)
