"""Model-agnostic LLM client for the meta-loop.

The meta-loop is bottlenecked on hypothesis quality, not call latency.
We want flexibility to swap models — Claude Opus 4.7 today, Qwen3 Max
via OpenRouter tomorrow, local Qwen3.6 via Ollama if the operator wants
offline.

Single interface: `complete(system, messages, schema) -> dict`. All
paths return a Python dict validated against the caller-supplied schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Protocol

Provider = Literal["anthropic", "openrouter", "ollama", "vllm"]


@dataclass
class MetaLLMConfig:
    provider: Provider = "anthropic"
    model: str = "claude-opus-4-7"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: Optional[str] = None      # for openrouter / ollama / vllm
    max_tokens: int = 4000
    temperature_disallowed: bool = True  # Opus 4.7 rejects temperature


class MetaLLMClient(Protocol):
    """All meta-loop LLM calls go through one of these."""

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        output_schema: Any,
    ) -> Dict[str, Any]:
        ...


# Implementations (anthropic_client.py, openrouter_client.py,
# ollama_client.py) live alongside this module and conform to the
# Protocol. The meta-loop accepts any.
