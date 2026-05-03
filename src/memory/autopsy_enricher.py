"""LLM-rewrite the templated TODO sections in a freshly written autopsy.

Cheap second-stage call (defaults to Sonnet 4.6). Reads the templated body,
sends it + raw trade events + triggering SR to the model, asks for two
fields: `what_actually_happened` and `one_line_lesson`. Replaces the TODO
sections in-place. Does not touch frontmatter or other body sections.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from src.llm.schema import SituationReport
from src.memory.markdown_store import MarkdownMemory, parse_frontmatter

logger = logging.getLogger(__name__)


class AutopsyEnrichment(BaseModel):
    what_actually_happened: str
    one_line_lesson: str


@dataclass
class EnricherConfig:
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1500
    enabled: bool = True


SECTION_HEADERS = {
    "what_actually_happened": "## What actually happened",
    "one_line_lesson": "## One-line lesson",
}

# All headers the autopsy_writer emits, in document order. Used as terminators
# when replacing a section so we never truncate at a `## ` that legitimately
# appears inside body prose (e.g. quoting a price level "## 2014.5 reaction").
KNOWN_AUTOPSY_HEADERS = (
    "## Outcome",
    "## Trade events",
    "## What the SITUATION REPORT predicted",
    "## What actually happened",
    "## One-line lesson",
)

SYSTEM_PROMPT = """\
You are the post-mortem analyst for ATLAS, a precision-trading system. Your
sole job is to read what was predicted, what actually happened, and write
the post-mortem in plain prose.

You will receive:
1. The autopsy markdown skeleton (with frontmatter + structured trade data
   + the SITUATION REPORT that triggered entry, if available)
2. The raw trade events as JSON

You return TWO fields:
- `what_actually_happened`: 4-8 sentences narrating the trade. Reference
  specific levels, the timing of exits, which lens called it correctly,
  which lens missed. Avoid hedge language. No filler.
- `one_line_lesson`: a single sentence the next SITUATION REPORT cycle
  should know. Specific. Actionable. If there is no lesson worth keeping,
  write \"No new lesson — the doctrine handled this correctly.\"

Match the ATLAS voice: short sentences, named parties, specific numbers.
Do not narrate your own reasoning process. Do not output anything outside
the schema.
"""


class AutopsyEnricher:
    def __init__(
        self,
        client: Optional[Any] = None,
        config: Optional[EnricherConfig] = None,
    ) -> None:
        self.config = config or EnricherConfig()
        if client is None:
            import anthropic  # local import
            client = anthropic.Anthropic()
        self.client = client

    def enrich(
        self,
        autopsy_path: Path,
        trade_events: List[Dict[str, Any]],
        triggering_sr: Optional[SituationReport] = None,
    ) -> Optional[Path]:
        """Rewrite the TODO sections of the autopsy file in place. Returns the path."""
        if not self.config.enabled:
            return None
        if not autopsy_path.exists():
            logger.warning("autopsy path missing: %s", autopsy_path)
            return None

        text = autopsy_path.read_text()
        try:
            enrichment = self._call(text, trade_events, triggering_sr)
        except Exception as e:  # noqa: BLE001 — never crash close path
            logger.exception("autopsy enrichment failed for %s: %s", autopsy_path, e)
            return None

        new_text = self._apply(text, enrichment)
        autopsy_path.write_text(new_text)
        logger.info("autopsy enriched: %s", autopsy_path)
        return autopsy_path

    # -- LLM call --------------------------------------------------------
    def _call(
        self,
        autopsy_text: str,
        trade_events: List[Dict[str, Any]],
        triggering_sr: Optional[SituationReport],
    ) -> AutopsyEnrichment:
        sr_block = "(no triggering SITUATION REPORT)"
        if triggering_sr is not None:
            sr_block = json.dumps(triggering_sr.model_dump(), indent=2, default=str)

        user_msg = (
            "AUTOPSY SKELETON (the file you will rewrite):\n"
            f"```markdown\n{autopsy_text}\n```\n\n"
            "RAW TRADE EVENTS (JSON):\n"
            f"```json\n{json.dumps(trade_events, indent=2, default=str)}\n```\n\n"
            "TRIGGERING SITUATION REPORT (JSON, if present):\n"
            f"```json\n{sr_block}\n```\n\n"
            "Produce {what_actually_happened, one_line_lesson} per the schema."
        )
        response = self.client.messages.parse(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            output_format=AutopsyEnrichment,
        )
        return response.parsed_output

    # -- in-place section replacement ------------------------------------
    @staticmethod
    def _apply(text: str, enrichment: AutopsyEnrichment) -> str:
        text = AutopsyEnricher._replace_section(
            text,
            SECTION_HEADERS["what_actually_happened"],
            enrichment.what_actually_happened.strip(),
        )
        text = AutopsyEnricher._replace_section(
            text,
            SECTION_HEADERS["one_line_lesson"],
            enrichment.one_line_lesson.strip(),
        )
        return text

    @staticmethod
    def _replace_section(text: str, header: str, body: str) -> str:
        """Replace everything from `header` until the next *known* header.

        Bounded to the KNOWN_AUTOPSY_HEADERS allow-list so a stray `## ` in
        the body prose (e.g. quoting a price level) cannot truncate.
        """
        # Find the header line. Anchor to start-of-line.
        pattern = re.compile(
            r"^" + re.escape(header) + r"[^\n]*\n",
            flags=re.MULTILINE,
        )
        match = pattern.search(text)
        if match is None:
            # Header missing — append the section
            return text.rstrip() + f"\n\n{header}\n\n{body}\n"

        body_start = match.end()
        # Search forward for the next known header (other than this one)
        end_idx = len(text)
        for next_header in KNOWN_AUTOPSY_HEADERS:
            if next_header == header:
                continue
            nh_match = re.search(
                r"^" + re.escape(next_header) + r"[^\n]*\n",
                text[body_start:],
                flags=re.MULTILINE,
            )
            if nh_match is not None:
                candidate = body_start + nh_match.start()
                if candidate < end_idx:
                    end_idx = candidate
        return text[:body_start] + body + "\n\n" + text[end_idx:]
