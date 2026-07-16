"""Tests for the vision chart analyst pipeline (mocked LLM — no API calls)."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.vision.chart_analyst import ChartAnalyst, AnalystConfig
from src.vision.chart_to_telegram import (
    ChartToTelegram,
    analysis_to_proposal,
    format_limit_order,
)
from src.vision.schema import ChartAnalysis, KeyLevel


# 1x1 PNG so the image-encoding path is exercised without a real chart.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def chart_png(tmp_path) -> Path:
    p = tmp_path / "chart.png"
    p.write_bytes(_PNG_1x1)
    return p


def _short_analysis(**over) -> ChartAnalysis:
    base = dict(
        instrument="XAUUSD", timeframe="H1", regime="downtrend",
        setup_present=True, direction="short",
        entry_limit=4128.0, stop=4140.0, targets=[4090.0, 4060.0],
        thesis="H1 downtrend, retracement into declining MA rejected; late longs trapped above",
        kill_thesis="H1 closes back above 4140 with momentum for two bars",
        trapped_party="late longs from the 4140 retracement",
        confidence=0.7,
        key_levels=[KeyLevel(price=4140, role="resistance")],
    )
    base.update(over)
    return ChartAnalysis(**base)


class _FakeResp:
    def __init__(self, parsed): self.parsed_output = parsed


class _FakeMessages:
    def __init__(self, parsed): self._p = parsed; self.last: Dict[str, Any] = {}
    def parse(self, **kw): self.last = kw; return _FakeResp(self._p)


class _FakeClient:
    def __init__(self, parsed): self.messages = _FakeMessages(parsed)


class _FakeTelegram:
    def __init__(self): self.sent: List[str] = []
    def send_message(self, text, urgent=False): self.sent.append(text); return True


# -- schema ---------------------------------------------------------------
def test_analysis_rr():
    a = _short_analysis()
    assert a.rr == pytest.approx((4128 - 4090) / (4140 - 4128))


# -- analyst sends image + cached mandate + schema ------------------------
def test_analyst_encodes_image_and_requests_schema(chart_png):
    client = _FakeClient(_short_analysis())
    analyst = ChartAnalyst(client=client, config=AnalystConfig())
    out = analyst.analyze_image(chart_png, instrument_hint="XAUUSD")
    assert out.direction == "short"
    kw = client.messages.last
    assert kw["model"] == "claude-opus-4-7"
    assert kw["output_format"] is ChartAnalysis
    # image block present + base64 data
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[0]["source"]["data"]  # non-empty base64
    # system prompt cached
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_analyst_rejects_unsupported_type(tmp_path):
    bad = tmp_path / "x.bmp"
    bad.write_bytes(b"nope")
    analyst = ChartAnalyst(client=_FakeClient(_short_analysis()))
    with pytest.raises(ValueError):
        analyst.analyze_image(bad)


# -- conversion + formatting ----------------------------------------------
def test_analysis_to_proposal_maps_fields():
    prop = analysis_to_proposal(_short_analysis())
    assert prop is not None
    assert prop.direction == "short"
    assert prop.entry == 4128.0 and prop.stop == 4140.0 and prop.target == 4090.0


def test_no_setup_yields_no_proposal():
    a = _short_analysis(setup_present=False, direction="none")
    assert analysis_to_proposal(a) is None


def test_format_limit_order_has_levels():
    msg = format_limit_order(_short_analysis())
    assert "LIMIT ORDER" in msg and "SHORT" in msg
    assert "4128" in msg and "4140" in msg and "4090" in msg
    assert "You place the order" in msg


def test_format_no_setup_message():
    msg = format_limit_order(_short_analysis(setup_present=False, direction="none"))
    assert "No high-quality setup" in msg


# -- pipeline: gate + telegram --------------------------------------------
def test_pipeline_sends_good_setup(chart_png):
    analyst = ChartAnalyst(client=_FakeClient(_short_analysis()))
    tg = _FakeTelegram()
    pipe = ChartToTelegram(analyst=analyst, telegram=tg)
    res = pipe.run(chart_png)
    assert res.decision == "SENT"
    assert res.sent is True
    assert len(tg.sent) == 1
    assert "LIMIT ORDER" in tg.sent[0]


def test_pipeline_blocks_bad_rr(chart_png):
    # tiny reward -> R:R below 2 -> discipline gate blocks -> not sent
    bad = _short_analysis(targets=[4126.0])
    analyst = ChartAnalyst(client=_FakeClient(bad))
    tg = _FakeTelegram()
    pipe = ChartToTelegram(analyst=analyst, telegram=tg)
    res = pipe.run(chart_png)
    assert res.decision == "BLOCKED"
    assert res.sent is False
    assert tg.sent == []


def test_pipeline_blocks_vague_kill(chart_png):
    bad = _short_analysis(kill_thesis="if it goes against me")
    analyst = ChartAnalyst(client=_FakeClient(bad))
    tg = _FakeTelegram()
    res = ChartToTelegram(analyst=analyst, telegram=tg).run(chart_png)
    assert res.decision == "BLOCKED"
    assert tg.sent == []


def test_pipeline_no_setup_does_not_send(chart_png):
    a = _short_analysis(setup_present=False, direction="none")
    analyst = ChartAnalyst(client=_FakeClient(a))
    tg = _FakeTelegram()
    res = ChartToTelegram(analyst=analyst, telegram=tg).run(chart_png)
    assert res.decision == "NO_SETUP"
    assert tg.sent == []


def test_pipeline_blocks_news_window(chart_png):
    analyst = ChartAnalyst(client=_FakeClient(_short_analysis()))
    tg = _FakeTelegram()
    res = ChartToTelegram(analyst=analyst, telegram=tg).run(chart_png, in_news_window=True)
    assert res.decision == "BLOCKED"
    assert tg.sent == []
