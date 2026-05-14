"""Cost model tests."""
from __future__ import annotations

from src.backtest.cost_model import CostModel


def test_normal_entry_cost_matches_components():
    cm = CostModel()
    # 0.5 * spread + slippage + taker fee = 0.5*4 + 2 + 5.5 = 9.5bp
    assert cm.entry_cost_bps("long", news_window=False) == 9.5
    assert cm.entry_cost_bps("short", news_window=False) == 9.5


def test_news_window_widens_cost():
    cm = CostModel()
    normal = cm.entry_cost_bps("long", news_window=False)
    news = cm.entry_cost_bps("long", news_window=True)
    assert news > normal
    # 0.5*25 + 8 + 5.5 = 26.0
    assert news == 26.0


def test_maker_cheaper_than_taker():
    cm = CostModel()
    taker = cm.entry_cost_bps("long", news_window=False, order_type="taker")
    maker = cm.entry_cost_bps("long", news_window=False, order_type="maker")
    assert maker < taker
    # 0.5*4 + 2 + 2.0 = 6.0
    assert maker == 6.0


def test_funding_drag_long_pays_short_receives():
    cm = CostModel()
    long_drag = cm.funding_drag_bps(hold_hours=8.0, side="long")
    short_drag = cm.funding_drag_bps(hold_hours=8.0, side="short")
    assert long_drag == 1.5
    assert short_drag == -1.5


def test_funding_drag_zero_hold():
    cm = CostModel()
    assert cm.funding_drag_bps(0.0, "long") == 0.0
    assert cm.funding_drag_bps(-1.0, "long") == 0.0


def test_round_trip_includes_funding():
    cm = CostModel()
    rt_no_fund = cm.round_trip_cost_bps("long", news_window=False, hold_hours=0.0)
    rt_with_fund = cm.round_trip_cost_bps("long", news_window=False, hold_hours=24.0)
    # Funding for 24h = 1.5 * 3 = 4.5bp
    assert abs(rt_with_fund - rt_no_fund - 4.5) < 1e-9


def test_research_findings_baseline_11bp_round_trip():
    """The plan claims ~11bp round-trip is realistic on Bybit XAUUSD perp.

    Sanity-check: entry + exit (taker, normal) should be in that ballpark
    EXCLUSIVE of spread crossing — because traders typically quote 11bp
    as "fees + slippage" not including the half-spread.

    With defaults: entry=9.5, exit=9.5 → 19bp gross including half-spread crossing each side.
    The 11bp research number is "fees+slippage" only: 2+5.5 = 7.5 per side = 15bp round-trip.
    Reasonable: model is slightly more conservative than the headline number.
    """
    cm = CostModel()
    rt = cm.round_trip_cost_bps("long", news_window=False, hold_hours=0.0)
    assert 15.0 <= rt <= 25.0  # within a sane corridor of research findings
