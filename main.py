#!/usr/bin/env python3
"""ATLAS — XAUUSD trading signal engine entry point.

LLM-driven pipeline: features → cheap gate → brain (when gate fires) →
SITUATION REPORT → if TAKE, deterministic TradeSignal → executor →
SQLite + markdown autopsy on close → digest rebuild.
"""
from __future__ import annotations

import functools
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
from src.llm.atlas_brain import AtlasBrain, BrainConfig
from src.llm.schema import SituationReport
from src.logging_mon.logger import setup_logging
from src.logging_mon.system_monitor import SystemMonitor
from src.memory.autopsy_enricher import AutopsyEnricher, EnricherConfig
from src.memory.autopsy_writer import AutopsyWriter
from src.memory.digest_builder import DigestBuilder
from src.memory.markdown_store import MarkdownMemory
from src.risk.position_manager import PositionManager
from src.risk.risk_calculator import RiskCalculator
from src.signals.gate import CycleGate, GateConfig
from src.signals.llm_signal_generator import LLMSignalGenerator
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
        self.memory = MarkdownMemory(root=config["MEMORY"]["root"])
        self.autopsy_writer = AutopsyWriter(self.memory)
        self.digest_builder = DigestBuilder(self.memory)
        self.error_handler = ErrorHandler(self.telegram)
        self.monitor = SystemMonitor(
            self.connector,
            self.telegram,
            log_dir=Path(config["LOGGING"]["file_path"]),
            stale_threshold_seconds=int(config["MONITOR"]["data_stale_threshold_seconds"]),
        )

        self.position_mgr = PositionManager(
            self.db,
            scale_out_pct=config["EXIT_RULES"]["scale_out_pct"],
            on_close=self._on_position_close,
        )
        self.executor = Executor(self.connector, self.position_mgr, self.telegram, config)

        self.risk_calc = RiskCalculator(
            account_risk_pct=config["TRADING"]["account_risk_pct"],
            sl_distance_pips=config["EXIT_RULES"]["sl_distance"],
            tp_distances_pips=config["EXIT_RULES"]["tp_distances"],
            max_units_per_trade=config["POSITION_SIZING"].get("max_units_per_trade"),
        )

        # The brain is optional — when disabled, the bot acts as a screener only.
        self.brain: Optional[AtlasBrain] = None
        self.signal_gen: Optional[LLMSignalGenerator] = None
        self.autopsy_enricher: Optional[AutopsyEnricher] = None
        if config["ANTHROPIC"].get("enabled"):
            brain_cfg = BrainConfig(
                model=config["ANTHROPIC"]["model"],
                effort=config["ANTHROPIC"]["effort"],
                max_tokens=int(config["ANTHROPIC"]["max_tokens"]),
                mandate_path=config["ANTHROPIC"]["mandate_path"],
                addendum_path=config["ANTHROPIC"].get("addendum_path"),
            )
            import anthropic
            anthropic_client = anthropic.Anthropic(api_key=config["ANTHROPIC"]["api_key"])
            self.brain = AtlasBrain(client=anthropic_client, config=brain_cfg)
            self.signal_gen = LLMSignalGenerator(
                brain=self.brain,
                memory=self.memory,
                risk_calc=self.risk_calc,
                instrument=self.symbol,
                min_signal_interval_seconds=int(
                    config["SIGNAL"].get("min_signal_interval_seconds", 1800)
                ),
            )
            if config["ANTHROPIC"].get("autopsy_enabled"):
                self.autopsy_enricher = AutopsyEnricher(
                    client=anthropic_client,
                    config=EnricherConfig(
                        model=config["ANTHROPIC"]["autopsy_model"],
                        enabled=True,
                    ),
                )

        self.gate = CycleGate(GateConfig(
            invoke_on_volume_spike=config["GATE"]["invoke_on_volume_spike"],
            invoke_on_bos=config["GATE"]["invoke_on_bos"],
            invoke_on_session_open=config["GATE"]["invoke_on_session_open"],
        ))

        # Last triggering SITUATION REPORT (for autopsy linkage). Keyed by position_id.
        self._sr_by_position: Dict[str, SituationReport] = {}

    # -- main loop --------------------------------------------------------
    def run(self) -> None:
        logger.info(
            "ATLAS starting on %s (testnet=%s, brain_enabled=%s)",
            self.symbol,
            self.config["BYBIT"].get("testnet"),
            self.brain is not None,
        )
        self.telegram.send_message("🚀 ATLAS bot started")
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

    # -- cycle ------------------------------------------------------------
    def _cycle(self) -> None:
        candles = self._fetch_all()
        if not candles:
            logger.warning("Skipping cycle: missing candles")
            return
        self.monitor.mark_data_update()

        if self.signal_gen is None:
            logger.debug("brain disabled — running monitor-only cycle")
            latest_price = candles["5m"][-1]["close"]
            self.executor.monitor_exits(latest_price=latest_price)
            return

        funding = self.connector.fetch_funding_rate(self.symbol)
        oi = self.connector.fetch_open_interest(self.symbol)
        feature_pack = self.signal_gen.build_feature_pack(
            candles_m5=candles["5m"],
            candles_m15=candles["15m"],
            candles_m30=candles["30m"],
            candles_h1=candles["1h"],
            funding_rate=funding,
            open_interest=oi,
        )

        invoke, reason = self.gate.should_invoke(feature_pack)
        if not invoke:
            logger.debug("gate skip (%s)", reason)
            self.executor.monitor_exits(latest_price=candles["5m"][-1]["close"])
            return

        logger.info("gate fired (%s) — invoking brain", reason)
        sr = self.signal_gen.generate(feature_pack)
        if sr is None:
            self.executor.monitor_exits(latest_price=candles["5m"][-1]["close"])
            return

        logger.info(
            "SR: %s decision=%s confidence=%.2f thesis=%s",
            sr.regime,
            sr.trade_proposal.decision,
            sr.confidence,
            sr.thesis[:120],
        )

        if sr.trade_proposal.decision == "TAKE":
            balance = self._account_balance()
            signal = self.signal_gen.situation_report_to_trade_signal(sr, balance)
            if signal is not None:
                position = self.executor.execute_entry(signal)
                if position is not None:
                    self._sr_by_position[position["position_id"]] = sr
                self.db.log_signal(signal.__dict__, fired=True)

        self.executor.monitor_exits(latest_price=candles["5m"][-1]["close"])

    # -- close hook -------------------------------------------------------
    def _on_position_close(
        self,
        position: Dict[str, Any],
        trade_events: list,
    ) -> None:
        sr = self._sr_by_position.pop(position["position_id"], None)
        try:
            path = self.autopsy_writer.from_closed_position(
                position=position,
                trade_events=trade_events,
                triggering_sr=sr,
                instrument=self.symbol,
            )
            if self.autopsy_enricher is not None:
                self.autopsy_enricher.enrich(
                    autopsy_path=path,
                    trade_events=trade_events,
                    triggering_sr=sr,
                )
            self.digest_builder.rebuild(last_n=int(self.config["MEMORY"]["digest_window"]))
        except Exception as e:  # noqa: BLE001 — don't crash the loop on memory errors
            logger.exception("autopsy/digest update failed: %s", e)

    # -- helpers ----------------------------------------------------------
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
