"""Tests for CFTC COT parser + cache + Lens 4 integration."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.data_layer.cot_feed import (
    COTConfig,
    COTFeed,
    COTSnapshot,
    parse_cot_text,
)
from src.features import intent as intent_feat


SAMPLE_COT_TEXT = """
Disaggregated Commitments of Traders - Futures Only

GOLD - COMMODITY EXCHANGE INC.
Code-088691
                Open Interest is   500,000

(as of December 31, 2024)
                            : Producer/Merchant   :  Swap Dealers     :   Managed Money     :  Other Reportables  :   Total Reportable  :   Non-Reportable
Long      Short              :   Long      Short  :   Long  Short Spr :   Long  Short Spr   :   Long  Short Spr   :    Long      Short  :    Long     Short
ALL       :    50,000   60,000  : 30,000  20,000   10,000 :  120,000  80,000  5,000 : 40,000 30,000 8,000 : 250,000   195,000  : 50,000   55,000
"""


def test_parse_cot_text_finds_gold():
    snap = parse_cot_text(SAMPLE_COT_TEXT)
    assert snap is not None
    assert snap.report_date == "2024-12-31"
    # commercial = producer/merchant + swap dealer
    # PM long 50k + SD long 30k = 80k commercial long
    # PM short 60k + SD short 20k = 80k commercial short
    assert snap.commercial_long == 80_000
    assert snap.commercial_short == 80_000
    assert snap.commercial_net == 0
    assert snap.managed_money_long == 120_000
    assert snap.managed_money_short == 80_000
    assert snap.managed_money_net == 40_000


def test_cot_snapshot_bias_classification():
    snap = COTSnapshot(
        report_date="2024-12-31",
        commercial_long=100, commercial_short=50, commercial_net=50,
        managed_money_long=200, managed_money_short=300, managed_money_net=-100,
        small_spec_long=10, small_spec_short=10, small_spec_net=0,
    )
    assert snap.commercial_bias == "long"
    assert snap.managed_money_bias == "short"


def test_cot_alignment_classification():
    # Commercials long, managed money short → "commercials_vs_specs"
    assert intent_feat.classify_cot_alignment("long", "short") == "commercials_vs_specs"
    # Both long → aligned
    assert intent_feat.classify_cot_alignment("long", "long") == "aligned"
    # Either neutral → None
    assert intent_feat.classify_cot_alignment("neutral", "long") is None
    assert intent_feat.classify_cot_alignment(None, "long") is None


def test_intent_extract_with_cot_snapshot():
    cot = COTSnapshot(
        report_date="2024-12-31",
        commercial_long=100_000, commercial_short=50_000, commercial_net=50_000,
        managed_money_long=200_000, managed_money_short=300_000, managed_money_net=-100_000,
        small_spec_long=10_000, small_spec_short=10_000, small_spec_net=0,
    )
    out = intent_feat.extract(funding_rate=0.0002, cot_snapshot=cot)
    assert out.funding_skew == "long-crowded"
    assert out.commercial_bias == "long"
    assert out.managed_money_bias == "short"
    assert out.cot_alignment == "commercials_vs_specs"
    assert out.cot_report_date == "2024-12-31"


def test_intent_extract_without_cot_keeps_funding_only():
    out = intent_feat.extract(funding_rate=0.0002)
    assert out.funding_skew == "long-crowded"
    assert out.cot_alignment is None
    assert out.commercial_bias is None


def test_cot_feed_uses_cache_when_fresh():
    with tempfile.TemporaryDirectory() as tmpdir:
        feed = COTFeed(COTConfig(cache_dir=tmpdir))
        # Pre-seed the cache
        cached = COTSnapshot(
            report_date="2024-12-31",
            commercial_long=1, commercial_short=1, commercial_net=0,
            managed_money_long=1, managed_money_short=1, managed_money_net=0,
            small_spec_long=1, small_spec_short=1, small_spec_net=0,
        )
        Path(tmpdir, "latest.json").write_text(json.dumps(cached.to_dict()))
        # Without forcing refresh, should read cache (no network call)
        out = feed.latest(force_refresh=False)
        assert out is not None
        assert out.report_date == "2024-12-31"


def test_cot_feed_returns_none_on_corrupt_cache():
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "latest.json").write_text("not json")
        feed = COTFeed(COTConfig(cache_dir=tmpdir))
        out = feed.latest(force_refresh=False)
        assert out is None


def test_parse_cot_text_returns_none_when_no_gold():
    text = "Some other commodity\nNo gold rows here\n"
    assert parse_cot_text(text) is None
