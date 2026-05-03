"""Tests for the Atlas brain — uses a mocked Anthropic client."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from src.llm.atlas_brain import AtlasBrain, BrainConfig
from src.llm.schema import (
    ConfidenceBreakdown,
    EntryZone,
    SituationReport,
    TradeProposal,
)


class _FakeUsage:
    input_tokens = 200
    cache_read_input_tokens = 4000
    cache_creation_input_tokens = 0
    output_tokens = 500


class _FakeResponse:
    def __init__(self, sr: SituationReport) -> None:
        self.parsed_output = sr
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, sr: SituationReport) -> None:
        self._sr = sr
        self.last_kwargs: Dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        return _FakeResponse(self._sr)


class _FakeClient:
    def __init__(self, sr: SituationReport) -> None:
        self.messages = _FakeMessages(sr)


def _example_sr() -> SituationReport:
    return SituationReport(
        timestamp="2026-05-02T18:14:00Z",
        instrument="XAUUSD",
        data_quality="FRESH",
        regime="accumulation",
        evidence=["CVD divergence on M5", "FVG at 2042 unfilled"],
        trapped_party=None,
        dominant_party=None,
        asymmetry="NONE",
        thesis="Mean revert from spike low after CVD turn.",
        trade_proposal=TradeProposal(
            decision="TAKE",
            direction="long",
            entry_zone=EntryZone(low=2041.5, high=2042.0),
            invalidation="M5 close back below 2040",
            first_target=2050.0,
            rr_minimum=3.0,
            size_pct=2.0,
        ),
        kill_thesis="If M5 closes below 2040 with CVD positive, exit at market.",
        confidence=0.72,
        confidence_breakdown=ConfidenceBreakdown(
            flow=0.20, structure=0.18, context=0.18, intent=0.16
        ),
        notes_to_future_atlas="Watch for similar pattern at NY open.",
    )


def test_brain_passes_cached_mandate_in_system():
    sr = _example_sr()
    client = _FakeClient(sr)
    mandate_path = Path(__file__).resolve().parent.parent / "prompts" / "ATLAS_MANDATE.md"
    cfg = BrainConfig(mandate_path=str(mandate_path))
    brain = AtlasBrain(client=client, config=cfg)

    out = brain.analyze({"foo": "bar"}, memory_digest="(empty)")

    assert out.trade_proposal.decision == "TAKE"
    kw = client.messages.last_kwargs
    assert kw["model"] == "claude-opus-4-7"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "high"}
    # System prompt is a list with cache_control on the mandate block
    assert isinstance(kw["system"], list)
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "ATLAS — THE MANDATE" in kw["system"][0]["text"]
    # Output schema is the Pydantic model
    assert kw["output_format"] is SituationReport


def test_brain_user_message_carries_features_and_digest():
    sr = _example_sr()
    client = _FakeClient(sr)
    mandate_path = Path(__file__).resolve().parent.parent / "prompts" / "ATLAS_MANDATE.md"
    cfg = BrainConfig(mandate_path=str(mandate_path))
    brain = AtlasBrain(client=client, config=cfg)

    feature_pack = {"lens_flow": {"cvd": -123}, "instrument": "XAUUSD"}
    digest = "# What I keep getting wrong\n- nothing yet\n"
    setups = ["### spike-low-recovery\nbody"]

    brain.analyze(feature_pack, digest, setups)

    user_text = client.messages.last_kwargs["messages"][0]["content"]
    assert "FEATURE PACK" in user_text
    assert "MEMORY DIGEST" in user_text
    assert "RELEVANT SETUP NOTES" in user_text
    assert "spike-low-recovery" in user_text
    assert "cvd" in user_text


def test_brain_returns_validated_pydantic_object():
    sr = _example_sr()
    client = _FakeClient(sr)
    mandate_path = Path(__file__).resolve().parent.parent / "prompts" / "ATLAS_MANDATE.md"
    brain = AtlasBrain(client=client, config=BrainConfig(mandate_path=str(mandate_path)))
    out = brain.analyze({}, "(empty)")
    assert isinstance(out, SituationReport)
    assert out.confidence == 0.72


def test_brain_concatenates_mandate_and_addendum():
    sr = _example_sr()
    client = _FakeClient(sr)
    repo = Path(__file__).resolve().parent.parent
    mandate_path = repo / "prompts" / "ATLAS_MANDATE.md"
    addendum_path = repo / "prompts" / "ATLAS_ADDENDUM.md"
    brain = AtlasBrain(
        client=client,
        config=BrainConfig(
            mandate_path=str(mandate_path),
            addendum_path=str(addendum_path),
        ),
    )
    # Cached prefix must be at least 4096 tokens (~14336 chars at 3.5 chars/tok)
    # to cache on Opus 4.7. Verify both files contributed.
    assert "ATLAS — THE MANDATE" in brain.cached_system
    assert "ATLAS — ADDENDUM" in brain.cached_system
    assert len(brain.cached_system) >= 14_500

    brain.analyze({}, "(empty)")
    system = client.messages.last_kwargs["system"]
    assert system[0]["text"] == brain.cached_system
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_brain_warns_when_prefix_below_threshold(tmp_path, caplog):
    sr = _example_sr()
    client = _FakeClient(sr)
    short_mandate = tmp_path / "tiny.md"
    short_mandate.write_text("# Tiny mandate\nThis will not cache.\n")
    with caplog.at_level("WARNING"):
        brain = AtlasBrain(
            client=client,
            config=BrainConfig(
                mandate_path=str(short_mandate),
                addendum_path=None,
            ),
        )
    assert any("cache will likely not engage" in r.message.lower() for r in caplog.records)
    # The brain still works; the warning is the deliverable.
    assert isinstance(brain.cached_system, str)
