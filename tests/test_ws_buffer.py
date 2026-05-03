"""Tests for the WS buffer + Lens 1 integration."""
from __future__ import annotations

import time

from src.data_layer.bybit_ws import (
    OrderBookLevel,
    Trade,
    WSBuffer,
    WSBufferConfig,
    book_levels_from_bybit_msg,
    trade_from_bybit_msg,
)
from src.features import flow as flow_feat


def _book(side: str, base: float, count: int = 5, step: float = 1.0):
    if side == "bids":
        return [OrderBookLevel(price=base - i * step, size=10.0) for i in range(count)]
    return [OrderBookLevel(price=base + i * step, size=10.0) for i in range(count)]


def test_cvd_signs_correctly():
    buf = WSBuffer()
    buf.update_book(_book("bids", 2000), _book("asks", 2001))
    now = int(time.time() * 1000)
    buf.push_trade(Trade(timestamp=now, price=2001.0, size=5.0, side="buy"))
    buf.push_trade(Trade(timestamp=now, price=2000.5, size=3.0, side="sell"))
    snap = buf.snapshot()
    assert snap["cvd_full"] == 2.0  # +5 - 3
    assert snap["trades_in_buffer"] == 2


def test_sweep_detection_when_trade_eats_multiple_levels():
    buf = WSBuffer(WSBufferConfig(sweep_levels=2))
    buf.update_book(_book("bids", 2000), _book("asks", 2001))  # asks at 2001..2005
    now = int(time.time() * 1000)
    # Buy aggressor that hits price 2003 — eats asks at 2001, 2002, 2003 (3 levels)
    buf.push_trade(Trade(timestamp=now, price=2003.0, size=20.0, side="buy"))
    snap = buf.snapshot()
    assert len(snap["sweeps_recent"]) == 1
    assert snap["sweeps_recent"][0]["levels"] >= 2


def test_no_sweep_for_small_trade():
    buf = WSBuffer(WSBufferConfig(sweep_levels=2))
    buf.update_book(_book("bids", 2000), _book("asks", 2001))
    now = int(time.time() * 1000)
    # Small trade hits only the top ask
    buf.push_trade(Trade(timestamp=now, price=2001.0, size=1.0, side="buy"))
    snap = buf.snapshot()
    assert snap["sweeps_recent"] == []


def test_large_print_detection():
    buf = WSBuffer(WSBufferConfig(large_print_multiplier=3.0))
    buf.update_book(_book("bids", 2000), _book("asks", 2001))
    now = int(time.time() * 1000)
    # Seed 50 small trades to set median
    for i in range(60):
        buf.push_trade(Trade(timestamp=now + i, price=2000.5, size=1.0, side="buy"))
    # Now a big print
    buf.push_trade(Trade(timestamp=now + 100, price=2001.0, size=20.0, side="buy"))
    snap = buf.snapshot()
    assert len(snap["large_prints_recent"]) == 1
    assert snap["large_prints_recent"][0]["ratio"] >= 3.0


def test_book_imbalance_sign():
    buf = WSBuffer()
    bids = [OrderBookLevel(price=2000, size=100.0)]
    asks = [OrderBookLevel(price=2001, size=10.0)]
    buf.update_book(bids, asks)
    snap = buf.snapshot()
    # 100 bid vs 10 ask → strongly positive imbalance
    assert snap["ob_imbalance"] > 0.5
    assert snap["ob_spread"] == 1.0


def test_flow_extract_overlays_ws_data():
    buf = WSBuffer()
    buf.update_book(_book("bids", 2000), _book("asks", 2001))
    now = int(time.time() * 1000)
    for _ in range(20):
        buf.push_trade(Trade(timestamp=now, price=2001.0, size=5.0, side="buy"))
    snap = buf.snapshot()

    # Candles say sells (down candles) but WS says buys
    candles = [
        {"timestamp": i * 60_000, "open": 2002, "high": 2003, "low": 2000, "close": 2000, "volume": 100}
        for i in range(30)
    ]
    out = flow_feat.extract(candles, ws_snapshot=snap)
    # Source should be ws+candles
    assert out.source == "ws+candles"
    # CVD overridden by WS — positive (buy aggressors), not the candle's negative
    assert out.cvd_recent > 0
    assert out.aggressor_bias == "buy"
    assert out.ob_imbalance is not None
    assert out.ob_spread == 1.0


def test_flow_extract_falls_back_when_ws_stale():
    buf = WSBuffer()
    snap = buf.snapshot()  # no trades, no book → ob_age_ms huge, no trades
    candles = [
        {"timestamp": i * 60_000, "open": 2000, "high": 2001, "low": 1999, "close": 2001, "volume": 100}
        for i in range(30)
    ]
    out = flow_feat.extract(candles, ws_snapshot=snap)
    # Should fall back to candle-derived path
    assert out.source == "candles"


def test_trade_from_bybit_msg_parses_well_formed():
    msg = {"T": 1_750_000_000_000, "p": "2042.5", "v": "1.5", "S": "Buy"}
    trade = trade_from_bybit_msg(msg)
    assert trade is not None
    assert trade.price == 2042.5
    assert trade.size == 1.5
    assert trade.side == "buy"


def test_trade_from_bybit_msg_returns_none_on_garbage():
    assert trade_from_bybit_msg({"missing": "fields"}) is None


def test_book_levels_from_bybit_msg_filters_zero_size():
    msg = {
        "b": [["2000", "10"], ["1999", "0"]],  # zero-size = remove
        "a": [["2001", "5"]],
    }
    bids, asks = book_levels_from_bybit_msg(msg)
    assert len(bids) == 1
    assert bids[0].price == 2000.0
    assert len(asks) == 1
