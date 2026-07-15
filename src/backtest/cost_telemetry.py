"""Cost telemetry for LLM-in-the-loop backtests.

The brain calls in Path A are the expensive part of the loop. To reason about
"is the LLM actually paying for itself?" (net_alpha_usd = filter_alpha - llm_cost)
we need a per-run token/cost tracker.

CostTracker reads the ``usage`` attribute of an Anthropic SDK response, which
exposes:

    input_tokens                 — uncached input
    cache_read_input_tokens      — served from cache (10x cheaper)
    cache_creation_input_tokens  — writing to cache (slightly more expensive)
    output_tokens                — generated

The USD estimate is a lightweight approximation using the published Opus 4.7
list prices in ``MODEL_PRICING``. Real invoices should be reconciled from
Anthropic's console; ``estimated_usd`` is a "am I in the right ballpark?" check
for the A/B harness, not an accounting tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens). Prices are indicative — swap to whichever model
# you actually configured on the brain. `default` is used when the model key is
# not recognized.
# ---------------------------------------------------------------------------
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,
        "cache_write": 18.75,
    },
    "claude-opus-4-5": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,
        "cache_write": 18.75,
    },
    "claude-sonnet-4-5": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.10,
        "cache_write": 1.25,
    },
    "default": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,
        "cache_write": 18.75,
    },
}


def _get_usage_field(usage: Any, name: str) -> int:
    """Read ``usage.name`` (SDK object) or ``usage[name]`` (plain dict), safely.

    Anthropic returns SDK objects in production and dicts in some test doubles.
    Missing fields fall back to 0 so we never crash on partial usage payloads.
    """
    if usage is None:
        return 0
    if isinstance(usage, dict):
        value = usage.get(name, 0)
    else:
        value = getattr(usage, name, 0)
    return int(value or 0)


@dataclass
class CostTracker:
    """Cumulative token/cost tracker across a single backtest run.

    All fields are simple counters so the object is trivially serialisable and
    can be attached to a BacktestResult / ABResult without special handling.
    """

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0       # cache_read (cheap reads)
    total_cache_write_tokens: int = 0  # cache_creation (expensive writes)
    total_calls: int = 0
    per_model: Dict[str, Dict[str, int]] = field(default_factory=dict)

    # ---------------- ingestion ----------------

    def record(self, response: Any, model: str = "claude-opus-4-7") -> None:
        """Fold one brain response's usage into the running totals.

        ``response`` may be:
            * an Anthropic SDK response (has ``.usage``),
            * a bare usage object (has ``input_tokens`` etc.),
            * a dict of usage counts.

        Unknown / missing shapes are silently treated as zero — this keeps the
        cost tracker resilient to mocks in tests. Fields recognised:
        ``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
        ``cache_creation_input_tokens``.
        """
        usage = getattr(response, "usage", response)
        input_tokens = _get_usage_field(usage, "input_tokens")
        output_tokens = _get_usage_field(usage, "output_tokens")
        cache_read = _get_usage_field(usage, "cache_read_input_tokens")
        cache_write = _get_usage_field(usage, "cache_creation_input_tokens")

        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cached_tokens += cache_read
        self.total_cache_write_tokens += cache_write
        self.total_calls += 1

        bucket = self.per_model.setdefault(
            model,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "calls": 0,
            },
        )
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_read_tokens"] += cache_read
        bucket["cache_write_tokens"] += cache_write
        bucket["calls"] += 1

    # ---------------- reporting ----------------

    def estimated_usd(self, model: str = "claude-opus-4-7") -> float:
        """Approximate USD cost of the calls tallied so far.

        If ``self.per_model`` has entries, we price each model bucket at its
        own rate. Otherwise (rare — happens when ``record`` was called without
        a model tag) we price all totals at ``model``'s rate.
        """
        if self.per_model:
            total = 0.0
            for m, b in self.per_model.items():
                total += _price(
                    m,
                    input_tokens=b["input_tokens"],
                    output_tokens=b["output_tokens"],
                    cache_read_tokens=b["cache_read_tokens"],
                    cache_write_tokens=b["cache_write_tokens"],
                )
            return total
        return _price(
            model,
            input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens,
            cache_read_tokens=self.total_cached_tokens,
            cache_write_tokens=self.total_cache_write_tokens,
        )

    def cache_hit_ratio(self) -> float:
        """Fraction of input tokens served from cache (0.0 when no calls)."""
        served = self.total_input_tokens + self.total_cached_tokens
        if served <= 0:
            return 0.0
        return self.total_cached_tokens / served

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "cache_hit_ratio": self.cache_hit_ratio(),
            "estimated_usd": self.estimated_usd(),
            "per_model": {k: dict(v) for k, v in self.per_model.items()},
        }


def _price(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float:
    rates = MODEL_PRICING.get(model, MODEL_PRICING["default"])
    per_mil = 1_000_000.0
    return (
        input_tokens * rates["input"] / per_mil
        + output_tokens * rates["output"] / per_mil
        + cache_read_tokens * rates["cache_read"] / per_mil
        + cache_write_tokens * rates["cache_write"] / per_mil
    )
