"""End-to-end signal generator tests with synthetic candles."""
from __future__ import annotations

from typing import Dict, List

from tests.conftest import base_config

from src.indicators.indicator_engine import IndicatorEngine
from src.patterns.spike_detector import SpikeDetector
from src.risk.risk_calculator import RiskCalculator
from src.signals.signal_generator import SignalGenerator


def _candle(ts, open_, high, low, close, vol=1000):
    return {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": vol}


def _build_spike_then_bounce_m5(start_ts: int = 0) -> List[Dict[str, float]]:
    """Construct a synthetic series: drift down → spike low → consolidation → bounce."""
    candles: List[Dict[str, float]] = []
    # 30 candles of stable drift to seed indicators
    price = 2000.0
    for i in range(30):
        ts = (start_ts + i) * 60_000
        candles.append(_candle(ts, price, price + 0.5, price - 0.5, price, vol=1000))

    # Spike: drop from 2000 → 1980 over 5 candles with surging volume
    spike_lows = [1995, 1990, 1985, 1982, 1980]
    for i, low in enumerate(spike_lows):
        ts = (start_ts + 30 + i) * 60_000
        op = candles[-1]["close"]
        cl = low + 1
        hi = max(op, cl) + 0.5
        candles.append(_candle(ts, op, hi, low, cl, vol=8000))

    # Consolidation: 5 candles in tight range above spike
    for i in range(5):
        ts = (start_ts + 35 + i) * 60_000
        candles.append(_candle(ts, 1981, 1982, 1980.5, 1981, vol=1500))

    # Bounce candle — close above MA50 of the recent series
    ts = (start_ts + 40) * 60_000
    candles.append(_candle(ts, 1981, 1985, 1981, 1985, vol=4000))
    return candles


def _flat_candles(price: float, count: int, start_ts: int = 0) -> List[Dict[str, float]]:
    return [
        _candle((start_ts + i) * 60_000, price, price + 0.5, price - 0.5, price, vol=1000)
        for i in range(count)
    ]


def _seed(engine: IndicatorEngine, candles: List[Dict[str, float]]) -> Dict:
    return engine.seed(candles)


def test_signal_fires_when_all_timeframes_align():
    cfg = base_config()
    cfg["INDICATORS"]["ma_fast"] = 5
    cfg["INDICATORS"]["ma_slow"] = 10
    cfg["ENTRY_RULES"]["min_spike_pips"] = 90
    cfg["ENTRY_RULES"]["consolidation_max_range_pips"] = 30

    candles_m5 = _build_spike_then_bounce_m5()
    candles_m15 = _flat_candles(1985, len(candles_m5))
    candles_m30 = _flat_candles(1985, len(candles_m5))
    candles_h1 = _flat_candles(1985, len(candles_m5))

    eng_m5 = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)
    eng_m15 = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)
    eng_m30 = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)
    eng_h1 = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)

    ind_m5 = _seed(eng_m5, candles_m5)
    ind_m15 = _seed(eng_m15, candles_m15)
    ind_m30 = _seed(eng_m30, candles_m30)
    ind_h1 = _seed(eng_h1, candles_h1)

    spike = SpikeDetector(
        min_spike_pips=cfg["ENTRY_RULES"]["min_spike_pips"],
        min_spike_candles=cfg["ENTRY_RULES"]["min_spike_candles"],
        consolidation_candles=cfg["ENTRY_RULES"]["consolidation_min_candles"],
        consolidation_max_range_pips=cfg["ENTRY_RULES"]["consolidation_max_range_pips"],
    )
    risk = RiskCalculator(
        account_risk_pct=cfg["TRADING"]["account_risk_pct"],
        sl_distance_pips=cfg["EXIT_RULES"]["sl_distance"],
        tp_distances_pips=cfg["EXIT_RULES"]["tp_distances"],
    )
    gen = SignalGenerator(cfg, risk)

    # Replay history through spike detector to set state appropriately:
    for i in range(len(candles_m5)):
        # Build cumulative indicator at each step, then drive spike detector
        # Use full seed() each time so AD reflects the full window.
        eng_tmp = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)
        ind_step = eng_tmp.seed(candles_m5[: i + 1])
        spike.update(candles_m5[: i + 1], ind_step)

    # Final pass through generator using already-seeded indicators
    signal = gen.generate(
        candles_m5=candles_m5,
        candles_m15=candles_m15,
        candles_m30=candles_m30,
        candles_h1=candles_h1,
        ind_m5=ind_m5,
        ind_m15=ind_m15,
        ind_m30=ind_m30,
        ind_h1=ind_h1,
        spike_detector=spike,
        account_balance=10_000.0,
    )
    assert signal is not None, "Signal expected when all timeframes align"
    assert signal.entry_price > 0
    assert signal.stop_loss < signal.entry_price
    assert signal.tp3 > signal.entry_price


def test_signal_blocked_when_h1_breaking_down():
    cfg = base_config()
    candles_m5 = _build_spike_then_bounce_m5()
    candles_m15 = _flat_candles(1985, len(candles_m5))
    candles_m30 = _flat_candles(1985, len(candles_m5))
    # H1: declining series — closes well below MA, MA falling
    candles_h1 = []
    for i in range(len(candles_m5)):
        price = 2050 - i * 0.5  # steady decline
        candles_h1.append(_candle(i * 60_000, price, price + 0.5, price - 0.5, price))

    eng_m5 = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)
    ind_m5 = eng_m5.seed(candles_m5)
    eng_m15 = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)
    ind_m15 = eng_m15.seed(candles_m15)
    eng_m30 = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)
    ind_m30 = eng_m30.seed(candles_m30)
    eng_h1 = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)
    ind_h1 = eng_h1.seed(candles_h1)

    spike = SpikeDetector(
        min_spike_pips=cfg["ENTRY_RULES"]["min_spike_pips"],
        min_spike_candles=cfg["ENTRY_RULES"]["min_spike_candles"],
        consolidation_candles=cfg["ENTRY_RULES"]["consolidation_min_candles"],
        consolidation_max_range_pips=cfg["ENTRY_RULES"]["consolidation_max_range_pips"],
    )
    risk = RiskCalculator(
        account_risk_pct=cfg["TRADING"]["account_risk_pct"],
        sl_distance_pips=cfg["EXIT_RULES"]["sl_distance"],
        tp_distances_pips=cfg["EXIT_RULES"]["tp_distances"],
    )
    gen = SignalGenerator(cfg, risk)

    for i in range(len(candles_m5)):
        eng_tmp = IndicatorEngine(ma_fast=5, ma_slow=10, ad_threshold=-1_000, volume_lookback=5)
        ind_step = eng_tmp.seed(candles_m5[: i + 1])
        spike.update(candles_m5[: i + 1], ind_step)

    signal = gen.generate(
        candles_m5=candles_m5,
        candles_m15=candles_m15,
        candles_m30=candles_m30,
        candles_h1=candles_h1,
        ind_m5=ind_m5,
        ind_m15=ind_m15,
        ind_m30=ind_m30,
        ind_h1=ind_h1,
        spike_detector=spike,
        account_balance=10_000.0,
    )
    assert signal is None, "H1 breakdown should suppress signal"
