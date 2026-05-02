"""LLM brain: cached mandate + feature pack + memory digest -> SITUATION REPORT.

Wires Anthropic SDK with prompt caching on the static mandate (system prompt)
and Pydantic-validated structured output. Volatile content (features, digest,
relevant setups) goes in the user message, after the cache breakpoint.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.llm.schema import SituationReport

logger = logging.getLogger(__name__)


@dataclass
class BrainConfig:
    model: str = "claude-opus-4-7"
    effort: str = "high"
    max_tokens: int = 16_000
    mandate_path: str = "prompts/ATLAS_MANDATE.md"


class AtlasBrain:
    """Single LLM call. The mandate is the cached system prompt."""

    def __init__(
        self,
        client: Optional[Any] = None,
        config: Optional[BrainConfig] = None,
    ) -> None:
        self.config = config or BrainConfig()
        if client is None:
            import anthropic  # local import keeps tests light
            client = anthropic.Anthropic()
        self.client = client
        self.mandate = Path(self.config.mandate_path).read_text()

    def analyze(
        self,
        feature_pack: Dict[str, Any],
        memory_digest: str,
        relevant_setups: Optional[List[str]] = None,
    ) -> SituationReport:
        """Produce a SITUATION REPORT for the given market state."""
        user_msg = self._format_input(feature_pack, memory_digest, relevant_setups or [])
        response = self.client.messages.parse(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self.config.effort},
            system=[
                {
                    "type": "text",
                    "text": self.mandate,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
            output_format=SituationReport,
        )
        self._log_usage(response)
        return response.parsed_output

    @staticmethod
    def _format_input(
        feature_pack: Dict[str, Any],
        memory_digest: str,
        relevant_setups: List[str],
    ) -> str:
        feat_json = json.dumps(feature_pack, indent=2, sort_keys=True, default=str)
        setup_block = (
            "\n\n".join(relevant_setups)
            if relevant_setups
            else "(none currently relevant)"
        )
        return (
            "FEATURE PACK — current state across the four lenses:\n"
            f"```json\n{feat_json}\n```\n\n"
            "MEMORY DIGEST — what ATLAS keeps getting right and wrong recently:\n"
            f"```markdown\n{memory_digest}\n```\n\n"
            "RELEVANT SETUP NOTES — taxonomy entries that match the current pattern:\n"
            f"{setup_block}\n\n"
            "Produce a SITUATION REPORT per the schema in your system prompt."
        )

    @staticmethod
    def _log_usage(response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        logger.info(
            "atlas_brain usage: input=%s cache_read=%s cache_create=%s output=%s",
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
