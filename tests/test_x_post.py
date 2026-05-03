"""Tests for the public-track X post helper."""
from __future__ import annotations

from src.llm.schema import (
    ConfidenceBreakdown,
    EntryZone,
    SituationReport,
    TradeProposal,
    TrappedParty,
)
from src.publishing.x_post import POST_CHAR_LIMIT, PostConfig, render_post, round_to


def _take_sr(price: float = 2042.0) -> SituationReport:
    return SituationReport(
        timestamp="2026-05-02T18:14:00Z",
        instrument="XAUUSD",
        data_quality="FRESH",
        regime="accumulation",
        evidence=["a", "b"],
        trapped_party=TrappedParty(who="late longs", level=2055.0, pain_bp=38.0),
        dominant_party=None,
        asymmetry="late longs trapped above 2055",
        thesis="Mean revert from spike low after CVD divergence and FVG fill.",
        trade_proposal=TradeProposal(
            decision="TAKE",
            direction="long",
            entry_zone=EntryZone(low=price - 0.5, high=price + 0.5),
            invalidation="below 2040",
            first_target=price * 1.003,
            rr_minimum=3.0,
            size_pct=2.0,
        ),
        kill_thesis="If M5 closes above 2055 with CVD positive, exit at market.",
        confidence=0.72,
        confidence_breakdown=ConfidenceBreakdown(flow=0.20, structure=0.18, context=0.18, intent=0.16),
        notes_to_future_atlas="watch NY open",
    )


def _no_trade_sr() -> SituationReport:
    sr = _take_sr()
    sr.trade_proposal = TradeProposal(
        decision="NO_TRADE",
        direction="n/a",
        entry_zone=None,
        invalidation="n/a",
        first_target=None,
        rr_minimum=0.0,
        size_pct=0.0,
    )
    return sr


def test_round_to_basic():
    assert round_to(2042.3, 5.0) == 2040.0
    assert round_to(2043.0, 5.0) == 2045.0
    assert round_to(100.0, 0.0) == 100.0  # disabled


def test_take_post_under_char_limit():
    posts = render_post(_take_sr())
    assert len(posts) >= 1
    for p in posts:
        assert len(p) <= POST_CHAR_LIMIT


def test_take_post_strips_specific_levels():
    posts = render_post(_take_sr(price=2042.3))
    body = "\n".join(posts)
    # Public output should contain a rounded entry, not the precise zone bounds
    assert "2041.8" not in body
    assert "2042.8" not in body
    assert "around" in body.lower()


def test_take_post_does_not_leak_kill_thesis_level():
    posts = render_post(_take_sr())
    body = "\n".join(posts)
    # Specific level "2055" from kill_thesis should NOT appear in the post
    assert "2055" not in body
    # Kill thesis class label should appear instead
    assert "structural" in body.lower() or "flow" in body.lower()


def test_no_trade_post_renders():
    posts = render_post(_no_trade_sr())
    assert len(posts) == 1
    assert "NO TRADE" in posts[0]
    assert "Default is NO" in posts[0]


def test_paper_label_can_be_removed():
    posts = render_post(_take_sr(), PostConfig(label_paper=False))
    assert "[PAPER]" not in posts[0]
    posts = render_post(_take_sr(), PostConfig(label_paper=True))
    assert "[PAPER]" in posts[0]


def test_no_forbidden_vocabulary_in_post():
    """Mandate §VI prohibits 'guaranteed', 'to the moon', etc.

    Sanity check the helper itself isn't injecting any.
    """
    posts = render_post(_take_sr())
    body = "\n".join(posts).lower()
    for word in ("guaranteed", "moon", "lambo", "easy money", "slam dunk"):
        assert word not in body
