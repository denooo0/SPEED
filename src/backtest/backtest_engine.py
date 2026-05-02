"""Minimal backtesting harness — replay historical OHLCV through the engine."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.indicators.indicator_engine import IndicatorEngine
from src.patterns.spike_detector import SpikeDetector
from src.risk.risk_calculator import RiskCalculator
from src.signals.signal_generator import SignalGenerator, TradeSignal

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    entry_price: float
    exit_price: float
    pnl: float
    duration_candles: int
    rule: str


@dataclass
class BacktestReport:
    total_trades: int
    winners: int
    losers: int
    win_rate: float
    avg_pnl: float
    final_balance: float
    trades: List[BacktestTrade] = field(default_factory=list)


class BacktestEngine:
    """Single-timeframe synthetic backtest. Multi-TF data should be aligned by caller."""

    def __init__(self, config: Dict[str, Any], starting_balance: float = 10_000.0) -> None:
        self.config = config
        self.starting_balance = starting_balance
        self.balance = starting_balance

    def run(
        self,
        candles_m5: List[Dict[str, float]],
        candles_m15: List[Dict[str, float]],
        candles_m30: List[Dict[str, float]],
        candles_h1: List[Dict[str, float]],
    ) -> BacktestReport:
        engines = {tf: IndicatorEngine(
            ma_fast=self.config["INDICATORS"]["ma_fast"],
            ma_slow=self.config["INDICATORS"]["ma_slow"],
            ad_threshold=self.config["INDICATORS"]["ad_extreme_threshold"],
            volume_lookback=self.config["INDICATORS"].get("volume_lookback", 20),
            volume_multiplier=self.config["INDICATORS"]["volume_spike_multiplier"],
        ) for tf in ("m5", "m15", "m30", "h1")}

        spike = SpikeDetector(
            min_spike_pips=self.config["ENTRY_RULES"]["min_spike_pips"],
            min_spike_candles=self.config["ENTRY_RULES"]["min_spike_candles"],
            consolidation_candles=self.config["ENTRY_RULES"]["consolidation_min_candles"],
            consolidation_max_range_pips=self.config["ENTRY_RULES"].get(
                "consolidation_max_range_pips", 20
            ),
        )
        risk = RiskCalculator(
            account_risk_pct=self.config["TRADING"]["account_risk_pct"],
            sl_distance_pips=self.config["EXIT_RULES"]["sl_distance"],
            tp_distances_pips=self.config["EXIT_RULES"]["tp_distances"],
        )
        gen = SignalGenerator(self.config, risk)
        gen.min_signal_interval = 0  # disable cooldown for backtest

        trades: List[BacktestTrade] = []
        open_signal: Optional[TradeSignal] = None
        open_index = 0

        max_i = min(len(candles_m5), len(candles_m15), len(candles_m30), len(candles_h1))
        for i in range(max_i):
            ind_m5 = engines["m5"].update(candles_m5[i])
            ind_m15 = engines["m15"].update(candles_m15[i])
            ind_m30 = engines["m30"].update(candles_m30[i])
            ind_h1 = engines["h1"].update(candles_h1[i])

            if open_signal is None:
                signal = gen.generate(
                    candles_m5[: i + 1],
                    candles_m15[: i + 1],
                    candles_m30[: i + 1],
                    candles_h1[: i + 1],
                    ind_m5, ind_m15, ind_m30, ind_h1,
                    spike,
                    self.balance,
                )
                if signal:
                    open_signal = signal
                    open_index = i
            else:
                bar = candles_m5[i]
                if bar["low"] <= open_signal.stop_loss:
                    pnl = (open_signal.stop_loss - open_signal.entry_price) * open_signal.position_size
                    self.balance += pnl
                    trades.append(BacktestTrade(
                        entry_price=open_signal.entry_price,
                        exit_price=open_signal.stop_loss,
                        pnl=pnl,
                        duration_candles=i - open_index,
                        rule="SL",
                    ))
                    open_signal = None
                elif bar["high"] >= open_signal.tp3:
                    pnl = (open_signal.tp3 - open_signal.entry_price) * open_signal.position_size
                    self.balance += pnl
                    trades.append(BacktestTrade(
                        entry_price=open_signal.entry_price,
                        exit_price=open_signal.tp3,
                        pnl=pnl,
                        duration_candles=i - open_index,
                        rule="TP3",
                    ))
                    open_signal = None

        winners = sum(1 for t in trades if t.pnl > 0)
        return BacktestReport(
            total_trades=len(trades),
            winners=winners,
            losers=len(trades) - winners,
            win_rate=(winners / len(trades)) if trades else 0.0,
            avg_pnl=(sum(t.pnl for t in trades) / len(trades)) if trades else 0.0,
            final_balance=self.balance,
            trades=trades,
        )
