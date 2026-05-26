"""Hypothesis forge: a raw observation -> one falsifiable Hypothesis.

The front door of the edge-generation pipeline (`plan/edge_generation_workflow.md`).
Mirrors `src/llm/atlas_brain.py`: the forge role (`prompts/ATLAS_FORGE.md`) plus the
four-lens definitions form the cached system block; the volatile observation and the
relevant setup taxonomy go in the user message after the cache breakpoint. Output is
a Pydantic-validated `Hypothesis`.

Owned by Track C per the note in `src/backtest/runner.py`. The Hypothesis it emits is
consumed by `BacktestRunner.evaluate_hypothesis` via `Hypothesis.to_runner_dict()`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.research.hypothesis import Hypothesis, new_hypothesis_id

logger = logging.getLogger(__name__)


@dataclass
class ForgeConfig:
    model: str = "claude-opus-4-7"
    effort: str = "high"
    max_tokens: int = 8_000
    role_path: str = "prompts/ATLAS_FORGE.md"
    # The four-lens definitions live in the mandate; we splice §III into the
    # cached prefix so the forge reasons in the same coordinate system the brain
    # does without loading the entire trading mandate.
    mandate_path: str = "prompts/ATLAS_MANDATE.md"


class HypothesisForge:
    """Single LLM call. The forge role (+ lens definitions) is the cached system."""

    def __init__(
        self,
        client: Optional[Any] = None,
        config: Optional[ForgeConfig] = None,
    ) -> None:
        self.config = config or ForgeConfig()
        if client is None:
            import anthropic  # local import keeps tests light

            client = anthropic.Anthropic()
        self.client = client
        self.cached_system = self._load_cached_system()

    def _load_cached_system(self) -> str:
        role = Path(self.config.role_path).read_text()
        lenses = self._extract_lens_section(Path(self.config.mandate_path))
        return role + ("\n\n---\n\n" + lenses if lenses else "")

    @staticmethod
    def _extract_lens_section(mandate_path: Path) -> str:
        """Pull `## III. THE FOUR LENSES` from the mandate, if present."""
        if not mandate_path.exists():
            logger.warning("mandate_path %s not found — forge runs without lens defs", mandate_path)
            return ""
        text = mandate_path.read_text()
        start = text.find("## III. THE FOUR LENSES")
        if start == -1:
            return ""
        # Section ends at the next top-level "## " heading.
        end = text.find("\n## ", start + 1)
        return text[start:end].strip() if end != -1 else text[start:].strip()

    def forge(
        self,
        observation: str,
        slug: str,
        parent_observation: Optional[str] = None,
        relevant_setups: Optional[List[str]] = None,
    ) -> Hypothesis:
        """Forge one Hypothesis from a raw observation.

        Parameters
        ----------
        observation:
            The operator's raw note (Notion card body or free text).
        slug:
            Short human hint used to mint the hypothesis id (e.g. 'ldn-pdh-sweep').
        parent_observation:
            Optional source id (e.g. the Notion page id) for provenance.
        relevant_setups:
            Setup taxonomy excerpts (from `MarkdownMemory.relevant_setups`).
        """
        user_msg = self._format_input(observation, relevant_setups or [])
        response = self.client.messages.parse(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self.config.effort},
            system=[
                {
                    "type": "text",
                    "text": self.cached_system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
            output_format=Hypothesis,
        )
        self._log_usage(response)
        hypothesis = response.parsed_output
        # The model may not mint a unique id; we own provenance and uniqueness.
        hypothesis.id = new_hypothesis_id(slug)
        if parent_observation is not None:
            hypothesis.parent_observation = parent_observation
        return hypothesis

    @staticmethod
    def _format_input(observation: str, relevant_setups: List[str]) -> str:
        setup_block = (
            "\n\n".join(relevant_setups)
            if relevant_setups
            else "(no codified setup matches this observation yet)"
        )
        return (
            "OPERATOR OBSERVATION — a raw, half-formed market hunch:\n"
            f"```\n{observation.strip()}\n```\n\n"
            "RELEVANT SETUP NOTES — codified patterns that may overlap:\n"
            f"{setup_block}\n\n"
            "Forge exactly one falsifiable Hypothesis per the schema and the laws "
            "in your system prompt. Name the mechanism. Pre-register the falsifiers. "
            "If there is no persistent mechanism, say so plainly in `mechanism`."
        )

    @staticmethod
    def _log_usage(response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        logger.info(
            "hypothesis_forge usage: input=%s cache_read=%s cache_create=%s output=%s",
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
