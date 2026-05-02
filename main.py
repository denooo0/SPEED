#!/usr/bin/env python3
"""ATLAS — XAUUSD trading signal engine entry point."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.config.config_loader import load_config
from src.data_layer.bybit_connector import BybitConnector
from src.database.db_manager import DatabaseManager
from src.errors.error_handler import ErrorHandler
from src.execution.executor import Executor
from src.indicators.indicator_engine import IndicatorEngine
from src.logging_mon.logger import setup_logging
from src.logging_mon.system_monitor import SystemMonitor
from src.patterns.spike_detector import SpikeDetector
from src.risk.position_manager import PositionManager
from src.risk.risk_calculator import RiskCalculator
from src.signals.signal_generator import SignalGenerator
from src.telegram_bot.bot import TelegramBot

logger = logging.getLogger("atlas.main")


class TradingBot:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.symbol = config["TRADING"]["symbol"]
        self.cycle_interval = int(config["MONITOR"].get("cycle_interval_seconds", 60))

        self.connector = BybitConnector(
            api_key=config["BYBIT"]["api_key"],
            api_secret=config["BYBIT"]["api_secret"],
            testnet=bool(config["BYBIT"].get("testnet", True)),
        )

        self.telegram = TelegramBot(config["TELEGRAM"])
        self.db = DatabaseManager(db_dir=config["DATABASE"]["path"])
        self.position_mgr = PositionManager(
            self.db, scale_out_pct=config["EXIT_RULES"]["scale_out_pct"]
        )
        self.executor = Executor(self.connector, self.position_mgr, self.telegram, config)
        self.error_handler = ErrorHandler(self.telegram)
        self.monitor = SystemMonitor(
            self.connector,
            self.telegram,
            log_dir=Path(config["LOGGING"]["file_path"]),
            stale_threshold_seconds=int(config["MONITOR"]["data_stale_threshold_seconds"]),
        )

        ind_kwargs = dict(
            ma_fast=config["INDICATORS"]["ma_fast"],
            ma_slow=config["INDICATORS"]["ma_slow"],
            ad_threshold=config["INDICATORS"]["ad_extreme_threshold"],
            volume_lookback=config["INDICATORS"].get("volume_lookback", 20),
            volume_multiplier=config["INDICATORS"]["volume_spike_multiplier"],
        )
        self.engines = {
            "5m": IndicatorEngine(**ind_kwargs),
            "15m": IndicatorEngine(**ind_kwargs),
            "30m": IndicatorEngine(**ind_kwargs),
            "1h": IndicatorEngine(**ind_kwargs),
        }
        self.spike_detector = SpikeDetector(
            min_spike_pips=config["ENTRY_RULES"]["min_spike_pips"],
            min_spike_candles=config["ENTRY_RULES"]["min_spike_candles"],
            consolidation_candles=config["ENTRY_RULES"]["consolidation_min_candles"],
            consolidation_max_range_pips=config["ENTRY_RULES"].get(
                "consolidation_max_range_pips", 20
            ),
        )
        self.risk_calc = RiskCalculator(
            account_risk_pct=config["TRADING"]["account_risk_pct"],
            sl_distance_pips=config["EXIT_RULES"]["sl_distance"],
            tp_distances_pips=config["EXIT_RULES"]["tp_distances"],
            max_units_per_trade=config["POSITION_SIZING"].get("max_units_per_trade"),
        )
        self.signal_gen = SignalGenerator(config, self.risk_calc)

    def run(self) -> None:
        logger.info("ATLAS starting on %s (testnet=%s)", self.symbol, self.config["BYBIT"].get("testnet"))
        self.telegram.send_message("🚀 ATLAS XAUUSD bot started")
        try:
            while True:
                try:
                    self._cycle()
                except KeyboardInterrupt:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.error_handler.handle(e, context="cycle")
                    time.sleep(min(self.cycle_interval * 5, 300))
                self.telegram.flush_queue()
                self.monitor.check(self.cycle_interval)
                time.sleep(self.cycle_interval)
        except KeyboardInterrupt:
            logger.info("Shutting down…")
            self.telegram.send_message("🛑 ATLAS bot stopped")
        finally:
            self.db.close()

    def _cycle(self) -> None:
        candles = self._fetch_all()
        if not candles:
            logger.warning("Skipping cycle: missing candles")
            return
        self.monitor.mark_data_update()

        ind = {tf: self.engines[tf].seed(c) for tf, c in candles.items()}
        balance = self._account_balance()
        signal = self.signal_gen.generate(
            candles_m5=candles["5m"],
            candles_m15=candles["15m"],
            candles_m30=candles["30m"],
            candles_h1=candles["1h"],
            ind_m5=ind["5m"],
            ind_m15=ind["15m"],
            ind_m30=ind["30m"],
            ind_h1=ind["1h"],
            spike_detector=self.spike_detector,
            account_balance=balance,
        )
        if signal:
            self.db.log_signal(signal.to_dict(), fired=True)
            self.executor.execute_entry(
                signal,
                ma_at_entry=ind["5m"].get("ma_fast"),
                ad_at_entry=ind["5m"].get("ad"),
            )
        latest_price = candles["5m"][-1]["close"] if candles["5m"] else None
        self.executor.monitor_exits(latest_price=latest_price)

    def _fetch_all(self) -> Optional[Dict[str, Any]]:
        out: Dict[str, Any] = {}
        for tf in ("5m", "15m", "30m", "1h"):
            data = self.connector.fetch_candles(self.symbol, tf, limit=200)
            if not data:
                return None
            out[tf] = data
        return out

    def _account_balance(self) -> float:
        bal = self.connector.fetch_account_balance()
        if bal and bal.get("total"):
            return float(bal["total"])
        logger.warning("Falling back to placeholder balance of 10,000")
        return 10_000.0


def main(argv: list[str]) -> int:
    config_path = argv[1] if len(argv) > 1 else "config.yaml"
    config = load_config(config_path)
    setup_logging(config["LOGGING"])
    bot = TradingBot(config)
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
