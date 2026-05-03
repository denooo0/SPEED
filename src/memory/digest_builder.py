"""Aggregate recent autopsies into the digest the brain reads each cycle.

Deterministic Phase 0 build: count wins/losses overall and per-tag, surface
recurring tag clusters, output markdown to `memory/digest.md`. LLM-rewrite
of the digest (turning counts into prose) can be layered on later.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

from src.memory.markdown_store import MarkdownMemory

logger = logging.getLogger(__name__)


class DigestBuilder:
    def __init__(self, memory: MarkdownMemory) -> None:
        self.memory = memory

    def rebuild(self, last_n: int = 30, write: bool = True) -> str:
        autopsies = self.memory.recent_autopsies(limit=last_n)
        if not autopsies:
            content = self._empty_digest()
        else:
            content = self._render(autopsies)
        if write:
            self.memory.write_digest(content)
        return content

    # -- rendering -------------------------------------------------------
    @staticmethod
    def _empty_digest() -> str:
        return (
            "# What ATLAS Keeps Getting Wrong\n\n"
            "(Memory empty. No autopsies yet. The system has not earned an opinion.)\n"
        )

    def _render(self, autopsies: List[Tuple[Dict[str, Any], str]]) -> str:
        n = len(autopsies)
        results = Counter(fm.get("result") for fm, _ in autopsies)
        wins = results.get("won", 0)
        losses = results.get("lost", 0)
        breakeven = results.get("breakeven", 0)
        skipped = results.get("skipped", 0)

        win_rate = (wins / (wins + losses)) if (wins + losses) else 0.0
        r_multiples = [fm.get("r_multiple") or 0.0 for fm, _ in autopsies if fm.get("r_multiple") is not None]
        avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0

        # Per-tag cluster
        by_tag: Dict[str, List[float]] = defaultdict(list)
        for fm, _ in autopsies:
            for tag in fm.get("tags") or []:
                if isinstance(tag, str):
                    by_tag[tag].append(float(fm.get("r_multiple") or 0))

        recurring_losses = [
            (tag, rs)
            for tag, rs in by_tag.items()
            if len(rs) >= 2 and sum(rs) < 0
        ]
        recurring_losses.sort(key=lambda kv: sum(kv[1]))

        recurring_wins = [
            (tag, rs)
            for tag, rs in by_tag.items()
            if len(rs) >= 2 and sum(rs) > 0
        ]
        recurring_wins.sort(key=lambda kv: -sum(kv[1]))

        lines: List[str] = []
        lines.append("# What ATLAS Keeps Getting Wrong\n")
        lines.append(
            f"Rolling window: last {n} closed positions. "
            f"Wins: {wins} | Losses: {losses} | Breakeven: {breakeven} | Skipped: {skipped}."
        )
        lines.append(f"Win rate (excl. breakeven/skip): **{win_rate:.0%}**")
        lines.append(f"Average R-multiple: **{avg_r:+.2f}R**\n")

        lines.append("## Patterns currently bleeding")
        if recurring_losses:
            for tag, rs in recurring_losses[:5]:
                lines.append(
                    f"- `{tag}` — {len(rs)} occurrences, total R: {sum(rs):+.2f}. "
                    f"Hold to higher bar until investigated."
                )
        else:
            lines.append("- (none — no tag has clustered losses yet)")
        lines.append("")

        lines.append("## Patterns currently working")
        if recurring_wins:
            for tag, rs in recurring_wins[:5]:
                lines.append(
                    f"- `{tag}` — {len(rs)} occurrences, total R: {sum(rs):+.2f}. "
                    f"Keep weight up; expect mean reversion."
                )
        else:
            lines.append("- (none — no tag has clustered wins yet)")
        lines.append("")

        lines.append("## Recent autopsies")
        for fm, _body in autopsies[-5:]:
            tag_str = ", ".join(t for t in (fm.get("tags") or []) if isinstance(t, str))
            lines.append(
                f"- `{fm.get('id')}` — {fm.get('result')} ({fm.get('r_multiple', 0):+.2f}R) "
                f"[{tag_str}]"
            )

        return "\n".join(lines) + "\n"
