"""Tests for backtest.cost_telemetry.CostTracker.

Uses simple stubs to imitate the Anthropic SDK response shape. No real API
calls are made.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.backtest.cost_telemetry import MODEL_PRICING, CostTracker


# ----- shared usage helpers -------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Response:
    usage: Any


def _r(**kwargs: int) -> _Response:
    return _Response(usage=_Usage(**kwargs))


# ----- tests ----------------------------------------------------------------


def test_tracker_starts_empty():
    t = CostTracker()
    assert t.total_calls == 0
    assert t.total_input_tokens == 0
    assert t.total_output_tokens == 0
    assert t.total_cached_tokens == 0
    assert t.total_cache_write_tokens == 0
    assert t.estimated_usd() == 0.0
    assert t.cache_hit_ratio() == 0.0


def test_tracker_records_usage_object():
    t = CostTracker()
    t.record(_r(input_tokens=100, output_tokens=250, cache_read_input_tokens=800))
    assert t.total_calls == 1
    assert t.total_input_tokens == 100
    assert t.total_output_tokens == 250
    assert t.total_cached_tokens == 800


def test_tracker_records_bare_usage_dict():
    """Some test doubles / logging paths pass a bare dict, not an SDK object."""
    t = CostTracker()
    t.record({
        "input_tokens": 50,
        "output_tokens": 75,
        "cache_read_input_tokens": 4000,
        "cache_creation_input_tokens": 100,
    })
    assert t.total_calls == 1
    assert t.total_input_tokens == 50
    assert t.total_cached_tokens == 4000
    assert t.total_cache_write_tokens == 100


def test_tracker_accumulates_across_calls():
    t = CostTracker()
    t.record(_r(input_tokens=100, output_tokens=50))
    t.record(_r(input_tokens=200, output_tokens=150, cache_read_input_tokens=500))
    t.record(_r(input_tokens=50))
    assert t.total_calls == 3
    assert t.total_input_tokens == 350
    assert t.total_output_tokens == 200
    assert t.total_cached_tokens == 500


def test_tracker_ignores_missing_fields_safely():
    """Malformed usage payloads must not crash the run."""
    t = CostTracker()

    class Bare:
        pass  # no usage attribute at all

    t.record(Bare())
    assert t.total_calls == 1
    assert t.total_input_tokens == 0


def test_estimated_usd_matches_manual_calc_opus():
    t = CostTracker()
    # 1M input tokens, 500K output, 2M cache reads on opus 4.7
    t.record(_r(
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_input_tokens=2_000_000,
        cache_creation_input_tokens=0,
    ), model="claude-opus-4-7")

    rates = MODEL_PRICING["claude-opus-4-7"]
    expected = rates["input"] + 0.5 * rates["output"] + 2.0 * rates["cache_read"]
    assert t.estimated_usd() == pytest.approx(expected)


def test_estimated_usd_falls_back_to_default_for_unknown_model():
    t = CostTracker()
    t.record(_r(input_tokens=1_000_000), model="claude-imaginary-99")
    default_rate = MODEL_PRICING["default"]["input"]
    assert t.estimated_usd("claude-imaginary-99") == pytest.approx(default_rate)


def test_cache_hit_ratio_calculation():
    t = CostTracker()
    # 900 cache reads, 100 uncached input → 90% hit ratio
    t.record(_r(input_tokens=100, cache_read_input_tokens=900))
    assert t.cache_hit_ratio() == pytest.approx(0.9)


def test_cache_hit_ratio_zero_when_no_input():
    t = CostTracker()
    t.record(_r(output_tokens=500))  # weird but possible under a mock
    assert t.cache_hit_ratio() == 0.0


def test_to_dict_serialisable():
    t = CostTracker()
    t.record(_r(input_tokens=10, output_tokens=20, cache_read_input_tokens=1000))
    d = t.to_dict()
    for key in (
        "total_calls",
        "total_input_tokens",
        "total_output_tokens",
        "total_cached_tokens",
        "total_cache_write_tokens",
        "cache_hit_ratio",
        "estimated_usd",
        "per_model",
    ):
        assert key in d
    assert d["total_calls"] == 1


def test_per_model_breakdown_prices_each_model_correctly():
    t = CostTracker()
    t.record(_r(input_tokens=1_000_000), model="claude-opus-4-7")
    t.record(_r(input_tokens=1_000_000), model="claude-haiku-4-5")
    expected = MODEL_PRICING["claude-opus-4-7"]["input"] + MODEL_PRICING["claude-haiku-4-5"]["input"]
    assert t.estimated_usd() == pytest.approx(expected)
