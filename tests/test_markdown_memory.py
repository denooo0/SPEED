"""Tests for the markdown memory store."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.memory.markdown_store import (
    AutopsyRecord,
    MarkdownMemory,
    parse_frontmatter,
)


@pytest.fixture
def memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield MarkdownMemory(root=Path(tmpdir))


def test_default_layout_created(memory):
    assert memory.autopsies_dir.exists()
    assert memory.setups_dir.exists()
    assert memory.regimes_dir.exists()
    assert memory.journal_dir.exists()
    assert memory.digest_path.exists()


def test_default_digest_when_empty(memory):
    digest = memory.read_digest()
    assert "(Memory empty" in digest


def test_write_and_read_autopsy_with_frontmatter(memory):
    record = AutopsyRecord(
        autopsy_id="2026-05-02-1432-xauusd-test",
        instrument="XAUUSD",
        setup="spike-low-recovery",
        regime="accumulation",
        result="lost",
        r_multiple=-1.0,
        kill_thesis_triggered=True,
        tags=["london", "lost", "kill-thesis-clean"],
        body="Body of the autopsy with [[wikilinks]] and details.",
    )
    path = memory.write_autopsy(record)
    assert path.exists()

    fm, body = memory.load_autopsy(path)
    assert fm["id"] == "2026-05-02-1432-xauusd-test"
    assert fm["result"] == "lost"
    assert fm["r_multiple"] == -1.0
    assert fm["kill_thesis_triggered"] is True
    assert fm["tags"] == ["london", "lost", "kill-thesis-clean"]
    assert "wikilinks" in body


def test_recent_autopsies_returns_in_order(memory):
    for i in range(3):
        memory.write_autopsy(
            AutopsyRecord(
                autopsy_id=f"2026-05-{i+1:02d}-test",
                instrument="XAUUSD",
                setup=None,
                regime=None,
                result="won",
                r_multiple=2.0,
                kill_thesis_triggered=False,
                tags=[],
                body=f"Autopsy {i}",
            )
        )
    recents = memory.recent_autopsies(limit=2)
    assert len(recents) == 2
    assert recents[-1][0]["id"].startswith("2026-05-03")


def test_relevant_setups_filters_by_tag(memory):
    memory.write_setup("london-reversal", "Body with #london tag and #reversal.")
    memory.write_setup("ny-open-sweep", "Body with #ny tag.")
    out = memory.relevant_setups(tags=["london"])
    assert len(out) == 1
    assert "london-reversal" in out[0]


def test_relevant_setups_returns_top_n_when_no_tags(memory):
    for i in range(5):
        memory.write_setup(f"setup-{i}", "Body of setup.")
    out = memory.relevant_setups(tags=None, max_results=3)
    assert len(out) == 3


def test_parse_frontmatter_no_block():
    fm, body = parse_frontmatter("# Just a doc, no frontmatter\n")
    assert fm == {}
    assert "Just a doc" in body


def test_parse_frontmatter_handles_invalid_json_gracefully():
    text = "---\nname: not-quoted-string\n---\n\nbody"
    fm, body = parse_frontmatter(text)
    assert fm.get("name") == "not-quoted-string"
    assert body.strip() == "body"
