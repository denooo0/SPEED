"""Tests for the autopsy LLM enricher (mocked client)."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from src.llm.schema import (
    ConfidenceBreakdown,
    EntryZone,
    SituationReport,
    TradeProposal,
)
from src.memory.autopsy_enricher import (
    AutopsyEnricher,
    AutopsyEnrichment,
    EnricherConfig,
)
from src.memory.autopsy_writer import AutopsyWriter
from src.memory.markdown_store import MarkdownMemory


@pytest.fixture
def memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield MarkdownMemory(root=Path(tmpdir))


def _example_sr():
    return SituationReport(
        timestamp="2026-05-02T18:14:00Z",
        instrument="XAUUSD",
        data_quality="FRESH",
        regime="accumulation",
        evidence=["x"],
        trapped_party=None,
        dominant_party=None,
        asymmetry="x",
        thesis="t",
        trade_proposal=TradeProposal(
            decision="TAKE", direction="long", entry_zone=EntryZone(low=2041.0, high=2042.0),
            invalidation="below 2040", first_target=2050.0, rr_minimum=3.0, size_pct=2.0,
        ),
        kill_thesis="k",
        confidence=0.7,
        confidence_breakdown=ConfidenceBreakdown(flow=0.2, structure=0.2, context=0.2, intent=0.1),
        notes_to_future_atlas="n",
    )


class _FakeResponse:
    def __init__(self, enrichment: AutopsyEnrichment):
        self.parsed_output = enrichment


class _FakeMessages:
    def __init__(self, enrichment: AutopsyEnrichment):
        self._enrichment = enrichment
        self.last_kwargs: Dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        return _FakeResponse(self._enrichment)


class _FakeClient:
    def __init__(self, enrichment: AutopsyEnrichment):
        self.messages = _FakeMessages(enrichment)


def _write_templated_autopsy(memory) -> Path:
    writer = AutopsyWriter(memory)
    pos = {
        "position_id": "deadbeef",
        "entry_price": 2000.0,
        "entry_time": 1714670000000,
        "entry_size": 1.0,
        "sl": 1995.0,
        "tp1": 2003.0,
        "tp2": 2006.0,
        "tp3": 2012.0,
        "status": "CLOSED",
    }
    events = [
        {"event": "ENTRY", "price": 2000.0, "size": 1.0, "pnl": None, "rule_matched": None, "position_id": "deadbeef"},
        {"event": "EXIT", "price": 1995.0, "size": 1.0, "pnl": -5.0, "rule_matched": "SL", "position_id": "deadbeef"},
    ]
    return writer.from_closed_position(pos, events, triggering_sr=_example_sr())


def test_enricher_replaces_what_actually_happened_section(memory):
    path = _write_templated_autopsy(memory)
    original = path.read_text()
    assert "(TODO — fill with post-mortem prose" in original
    assert "(TODO — single sentence" in original

    enrichment = AutopsyEnrichment(
        what_actually_happened=(
            "Spike low at 1995 held for two M5 bars; CVD flipped positive on the third. "
            "Exit fired at 1995 SL on the candle that briefly tagged the level. The kill "
            "thesis was clean — price did exactly what it was contracted to do."
        ),
        one_line_lesson=(
            "London-session reversals at the spike low need an extra M15 confirmation "
            "before entry; this one front-ran the structure."
        ),
    )
    client = _FakeClient(enrichment)
    enricher = AutopsyEnricher(client=client, config=EnricherConfig(enabled=True))

    enricher.enrich(autopsy_path=path, trade_events=[], triggering_sr=_example_sr())

    new_text = path.read_text()
    assert "(TODO — fill with post-mortem prose" not in new_text
    assert "(TODO — single sentence" not in new_text
    assert "Spike low at 1995 held" in new_text
    assert "London-session reversals" in new_text
    # The frontmatter and other sections should still be present
    assert "## Outcome" in new_text
    assert "## Trade events" in new_text


def test_enricher_skips_when_disabled(memory):
    path = _write_templated_autopsy(memory)
    original = path.read_text()
    enrichment = AutopsyEnrichment(
        what_actually_happened="should not be applied",
        one_line_lesson="should not be applied",
    )
    client = _FakeClient(enrichment)
    enricher = AutopsyEnricher(client=client, config=EnricherConfig(enabled=False))
    out = enricher.enrich(autopsy_path=path, trade_events=[], triggering_sr=None)
    assert out is None
    assert path.read_text() == original


def test_enricher_handles_missing_file_gracefully(memory):
    enrichment = AutopsyEnrichment(what_actually_happened="x", one_line_lesson="y")
    client = _FakeClient(enrichment)
    enricher = AutopsyEnricher(client=client, config=EnricherConfig(enabled=True))
    out = enricher.enrich(
        autopsy_path=memory.autopsies_dir / "nonexistent.md",
        trade_events=[],
        triggering_sr=None,
    )
    assert out is None


def test_enricher_appends_section_if_header_missing(memory):
    # Hand-write an autopsy without the standard headers
    path = memory.autopsies_dir / "manual.md"
    path.write_text("---\nid: manual\n---\n\n## Outcome\n\nbody only.\n")
    enrichment = AutopsyEnrichment(
        what_actually_happened="appended_what",
        one_line_lesson="appended_lesson",
    )
    client = _FakeClient(enrichment)
    enricher = AutopsyEnricher(client=client, config=EnricherConfig(enabled=True))
    enricher.enrich(autopsy_path=path, trade_events=[], triggering_sr=None)
    text = path.read_text()
    assert "## What actually happened" in text
    assert "appended_what" in text
    assert "## One-line lesson" in text
    assert "appended_lesson" in text


def test_enricher_uses_configured_model(memory):
    path = _write_templated_autopsy(memory)
    enrichment = AutopsyEnrichment(what_actually_happened="x", one_line_lesson="y")
    client = _FakeClient(enrichment)
    enricher = AutopsyEnricher(
        client=client,
        config=EnricherConfig(model="claude-haiku-4-5", enabled=True),
    )
    enricher.enrich(autopsy_path=path, trade_events=[], triggering_sr=None)
    assert client.messages.last_kwargs["model"] == "claude-haiku-4-5"
    assert client.messages.last_kwargs["output_format"] is AutopsyEnrichment


def test_enricher_does_not_truncate_on_hash_in_body(memory):
    """Regression: the old `^## ` regex truncated when body prose contained
    `## ` (e.g. quoting a price level). The fix bounds replacement to
    known-header allow-list."""
    path = _write_templated_autopsy(memory)
    enrichment = AutopsyEnrichment(
        what_actually_happened=(
            "Bounce held at the FVG. Quoting the precise reaction level: "
            "## 2014.5 was the line. CVD flipped positive immediately after. "
            "Exit fired clean."
        ),
        one_line_lesson="Watch the precise FVG midpoint on M5 for reaction.",
    )
    client = _FakeClient(enrichment)
    enricher = AutopsyEnricher(client=client, config=EnricherConfig(enabled=True))
    enricher.enrich(autopsy_path=path, trade_events=[], triggering_sr=None)

    text = path.read_text()
    # The "## 2014.5 was the line." substring inside the body must NOT have
    # truncated the section. The lesson section must follow.
    assert "## 2014.5 was the line." in text
    assert "## One-line lesson" in text
    assert "Watch the precise FVG midpoint" in text
    # Section ordering preserved — "What actually happened" comes BEFORE "One-line lesson"
    assert text.index("## What actually happened") < text.index("## One-line lesson")


def test_enricher_double_pass_does_not_corrupt(memory):
    """Two enrichment passes (e.g. retried after a crash) must converge."""
    path = _write_templated_autopsy(memory)
    enrichment = AutopsyEnrichment(
        what_actually_happened="First-pass narrative.",
        one_line_lesson="First-pass lesson.",
    )
    client = _FakeClient(enrichment)
    enricher = AutopsyEnricher(client=client, config=EnricherConfig(enabled=True))

    enricher.enrich(autopsy_path=path, trade_events=[], triggering_sr=None)
    first_text = path.read_text()
    # Second pass with new content
    client.messages._enrichment = AutopsyEnrichment(
        what_actually_happened="Second-pass narrative replacing the first.",
        one_line_lesson="Second-pass lesson replacing the first.",
    )
    enricher.enrich(autopsy_path=path, trade_events=[], triggering_sr=None)
    second_text = path.read_text()

    # First-pass content gone, second-pass present, structure intact
    assert "First-pass narrative" not in second_text
    assert "Second-pass narrative" in second_text
    assert second_text.count("## What actually happened") == 1
    assert second_text.count("## One-line lesson") == 1
