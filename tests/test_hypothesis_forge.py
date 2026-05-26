"""Tests for the hypothesis forge and store — uses a mocked Anthropic client."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from src.research.hypothesis import (
    EntryRule,
    ExitRule,
    ExpectedEdge,
    Filters,
    Hypothesis,
    new_hypothesis_id,
)
from src.research.hypothesis_forge import ForgeConfig, HypothesisForge
from src.research.store import HypothesisStore

REPO = Path(__file__).resolve().parent.parent


class _FakeUsage:
    input_tokens = 150
    cache_read_input_tokens = 4000
    cache_creation_input_tokens = 0
    output_tokens = 400


class _FakeResponse:
    def __init__(self, h: Hypothesis) -> None:
        self.parsed_output = h
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, h: Hypothesis) -> None:
        self._h = h
        self.last_kwargs: Dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        return _FakeResponse(self._h)


class _FakeClient:
    def __init__(self, h: Hypothesis) -> None:
        self.messages = _FakeMessages(h)


def _example_hypothesis() -> Hypothesis:
    return Hypothesis(
        id="placeholder",
        thesis="London PDH sweep reverses into the NY open.",
        mechanism=(
            "Late London longs that chased the PDH sweep are stuck above it with "
            "thinning bids and must cover into the NY open, pressing price back down."
        ),
        lenses=["structure", "context"],
        entry_rule=EntryRule(
            trigger="high > pdh and 08:00 <= time <= 09:00 UTC, then M5 close back below pdh",
            features_required=["pdh", "london_open_window", "m5_close"],
        ),
        exit_rule=ExitRule(
            stop="sweep high + 0.3 ATR",
            target="pdh - 1R or session VWAP",
            time_stop="no reclaim within 6 bars",
        ),
        filters=Filters(session=["London"], regime=["distribution"], cot_state=None),
        features_used=["pdh", "atr", "session_vwap"],
        expected_edge=ExpectedEdge(direction="mean_revert", horizon_bars=12),
        falsifiers=["no reversal within 6 bars", "reversal-leg volume below sweep-leg volume"],
        min_sample=60,
    )


def _forge(client: _FakeClient) -> HypothesisForge:
    cfg = ForgeConfig(
        role_path=str(REPO / "prompts" / "ATLAS_FORGE.md"),
        mandate_path=str(REPO / "prompts" / "ATLAS_MANDATE.md"),
    )
    return HypothesisForge(client=client, config=cfg)


def test_forge_caches_role_and_lens_definitions():
    client = _FakeClient(_example_hypothesis())
    forge = _forge(client)
    assert "ATLAS — THE FORGE" in forge.cached_system
    # §III lens section is spliced in from the mandate.
    assert "THE FOUR LENSES" in forge.cached_system
    assert "LENS 1 — FLOW" in forge.cached_system


def test_forge_passes_cached_system_and_schema():
    client = _FakeClient(_example_hypothesis())
    forge = _forge(client)
    forge.forge("XAUUSD sweeps PDH in London hour-1 then reverses.", slug="ldn-pdh-sweep")

    kw = client.messages.last_kwargs
    assert kw["model"] == "claude-opus-4-7"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "high"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["output_format"] is Hypothesis


def test_forge_user_message_carries_observation_and_setups():
    client = _FakeClient(_example_hypothesis())
    forge = _forge(client)
    forge.forge(
        "XAUUSD sweeps PDH in London then reverses.",
        slug="ldn-pdh-sweep",
        relevant_setups=["### spike-low-recovery\nbody text"],
    )
    user_text = client.messages.last_kwargs["messages"][0]["content"]
    assert "OPERATOR OBSERVATION" in user_text
    assert "sweeps PDH in London" in user_text
    assert "spike-low-recovery" in user_text


def test_forge_mints_id_and_provenance():
    client = _FakeClient(_example_hypothesis())
    forge = _forge(client)
    h = forge.forge("note", slug="ldn-pdh-sweep", parent_observation="notion-page-42")
    assert h.id.startswith("H-")
    assert "ldn-pdh-sweep" in h.id
    assert h.parent_observation == "notion-page-42"


def test_hypothesis_rejects_thin_mechanism():
    with pytest.raises(ValueError, match="mechanism too thin"):
        Hypothesis(
            id="x",
            thesis="t",
            mechanism="reverses",
            lenses=["flow"],
            entry_rule=EntryRule(trigger="x"),
            exit_rule=ExitRule(stop="s", target="t"),
            expected_edge=ExpectedEdge(direction="mean_revert", horizon_bars=5),
            falsifiers=["dies if no reversal"],
        )


def test_hypothesis_requires_falsifier():
    with pytest.raises(ValueError, match="falsifier is required"):
        Hypothesis(
            id="x",
            thesis="t",
            mechanism="late longs are trapped above pdh and must cover into thin bids",
            lenses=["structure"],
            entry_rule=EntryRule(trigger="x"),
            exit_rule=ExitRule(stop="s", target="t"),
            expected_edge=ExpectedEdge(direction="mean_revert", horizon_bars=5),
            falsifiers=[],
        )


def test_store_save_load_roundtrip_and_runner_shape(tmp_path):
    store = HypothesisStore(root=tmp_path)
    h = _example_hypothesis()
    h.id = new_hypothesis_id("ldn-pdh-sweep")
    saved = store.save(h)
    assert saved.exists() and saved.suffix == ".yml"

    loaded = store.load(h.id)
    assert loaded.thesis == h.thesis
    assert loaded.expected_edge.horizon_bars == 12
    # The runner consumes a dict with at least an 'id'.
    assert loaded.to_runner_dict()["id"] == h.id


def test_store_retire_moves_and_records_reasons(tmp_path):
    store = HypothesisStore(root=tmp_path)
    h = _example_hypothesis()
    h.id = new_hypothesis_id("ldn-pdh-sweep")
    store.save(h)
    retired = store.retire(h, reasons=["pbo 0.31 >= 0.20", "dsr 0.81 <= 0.95"])

    assert retired.exists() and retired.suffix == ".md"
    assert not (store.hypotheses_dir / f"{h.id}.yml").exists()
    text = retired.read_text()
    assert "pbo 0.31" in text
    assert "Re-test" in text
