"""Vision chart analyst — Opus 4.7 reads a chart image and proposes a limit order.

Uses the ATLAS mandate as the cached system prompt and Opus 4.7 high-resolution
vision to read the chart the way a discretionary trader would, then returns a
structured ChartAnalysis (Pydantic-validated). The output is an ANALYSIS +
proposed LIMIT order — not an executed trade. It flows through the discipline
gate and out to Telegram for the operator to place.

Honest scope: this is discretionary visual analysis, not a backtested edge.
The mandate's discipline (named trapped party, specific kill thesis, R:R) is
enforced downstream by the copilot discipline gate before anything is sent.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.vision.schema import ChartAnalysis

logger = logging.getLogger(__name__)

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_ANALYST_SYSTEM = """\
You are ATLAS in chart-reading mode. You are shown a single price chart image.
Read it the way a disciplined discretionary trader would and propose AT MOST
one limit-order setup. You are advising a human who will place the order — you
never execute.

Rules:
- If there is no high-quality setup, set setup_present=false and direction=none.
  The default is NO. Do not invent a trade.
- When you DO propose a trade, it must satisfy the doctrine:
  * A named trapped party (who is caught, and where).
  * A single, observable kill thesis (what specific event voids the trade).
  * Risk:reward of at least 2:1 to the first target.
  * A limit entry (a price you'd want to be filled at), a stop beyond the
    invalidation, and 1-3 targets.
- Read the actual axis values from the chart for entry/stop/target. If you
  cannot read precise prices, say so in notes and lower confidence.
- Confidence is 0..1 and caps at 0.9. Round numbers above 0.85 are suspect.
- Be specific and terse. No hype words.
"""


@dataclass
class AnalystConfig:
    model: str = "claude-opus-4-7"
    effort: str = "high"
    max_tokens: int = 8000


class ChartAnalyst:
    def __init__(
        self,
        client: Optional[Any] = None,
        config: Optional[AnalystConfig] = None,
    ) -> None:
        self.config = config or AnalystConfig()
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client

    def analyze_image(
        self,
        image_path: str | Path,
        instrument_hint: str = "XAUUSD",
        extra_context: str = "",
    ) -> ChartAnalysis:
        image_path = Path(image_path)
        media_type = _MEDIA_TYPES.get(image_path.suffix.lower())
        if media_type is None:
            raise ValueError(f"Unsupported image type: {image_path.suffix}")
        data = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")

        user_text = (
            f"Instrument (if unreadable from the chart): {instrument_hint}.\n"
            f"{extra_context}\n"
            "Analyze this chart and return a ChartAnalysis. If there is no "
            "high-quality setup, setup_present=false."
        )
        response = self.client.messages.parse(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self.config.effort},
            system=[{
                "type": "text",
                "text": _ANALYST_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": data,
                    }},
                    {"type": "text", "text": user_text},
                ],
            }],
            output_format=ChartAnalysis,
        )
        return response.parsed_output
