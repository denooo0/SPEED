"""LLM-driven signal generator.

Composes the four-lens feature pack, fetches relevant memory, calls the
Atlas brain, and converts a TAKE SITUATION REPORT into a deterministic
TradeSignal via the existing RiskCalculator.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from src.features import context as ctx_feat
from src.features import flow as flow_feat
from src.features import intent as intent_feat
from src.features import structure as struct_feat
from src.llm.atlas_brain import AtlasBrain
from src.llm.schema import SituationReport
from src.memory.markdown_store import MarkdownMemory
from src.risk.risk_calculator import RiskCalculator
from src.signals.signal_generator import TradeSignal

logger = logging.getLogger(__name__)


class LLMSignalGenerator:
    """Compose features → call brain → convert TAKE to TradeSignal."""

    def __init__(
        self,
        brain: AtlasBrain,
        memory: MarkdownMemory,
        risk_calc: RiskCalculator,
        instrument: str,
        min_signal_interval_seconds: int = 1800,
    ) -> None:
        self.brain = brain
        self.memory = memory
        self.risk_calc = risk_calc
        self.instrument = instrument
        self.min_signal_interval = int(min_signal_interval_seconds)
        self.last_call_ts = 0.0

    # -- feature pack ----------------------------------------------------
    def build_feature_pack(
        self,
        candles_m5: List[Dict[str, float]],
        candles_m15: List[Dict[str, float]],
        candles_m30: List[Dict[str, float]],
        candles_h1: List[Dict[str, float]],
        funding_rate: Optional[float] = None,
        open_interest: Optional[float] = None,
        oi_1h_ago: Optional[float] = None,
    ) -> Dict[str, Any]:
        pack: Dict[str, Any] = {
            "instrument": self.instrument,
            "now_ms": int(time.time() * 1000),
            "lens_flow": {
                "m5": flow_feat.extract(candles_m5).to_dict(),
                "m15": flow_feat.extract(candles_m15).to_dict(),
            },
            "lens_structure": {
                "m5": struct_feat.extract(candles_m5).to_dict(),
                "m15": struct_feat.extract(candles_m15).to_dict(),
                "h1": struct_feat.extract(candles_h1).to_dict(),
            },
            "lens_context": ctx_feat.extract(candles_m5).to_dict(),
            "lens_intent": intent_feat.extract(
                funding_rate, open_interest, oi_1h_ago
            ).to_dict(),
            "latest_price": {
                "m5_close": candles_m5[-1]["close"] if candles_m5 else None,
                "m5_ts": candles_m5[-1]["timestamp"] if candles_m5 else None,
            },
        }
        return pack

    # -- main entry ------------------------------------------------------
    def generate(
        self,
        feature_pack: Dict[str, Any],
        force: bool = False,
    ) -> Optional[SituationReport]:
        """Run the brain. Returns the SR; caller decides what to do with TAKE."""
        now = time.time()
        if not force and (now - self.last_call_ts) < self.min_signal_interval:
            return None
        digest = self.memory.read_digest()
        relevant = self._relevant_setups(feature_pack)
        try:
            sr = self.brain.analyze(feature_pack, digest, relevant)
        except Exception as exc:  # noqa: BLE001 — surface to caller via log
            logger.exception("brain.analyze failed: %s", exc)
            return None
        self.last_call_ts = now
        return sr

    # -- conversion ------------------------------------------------------
    def situation_report_to_trade_signal(
        self,
        sr: SituationReport,
        account_balance: float,
    ) -> Optional[TradeSignal]:
        """Convert a TAKE SR into a deterministic TradeSignal.

        The brain provides the strategist's view (entry zone, first target,
        kill thesis). The risk calc enforces the discipline: SL distance,
        position size, R:R-derived TP2/TP3.
        """
        tp = sr.trade_proposal
        if tp.decision != "TAKE":
            return None
        if tp.entry_zone is None or tp.first_target is None:
            return None
        entry = (tp.entry_zone.low + tp.entry_zone.high) / 2
        params = self.risk_calc.build(entry=entry, account_balance=account_balance)
        # Brain's first_target overrides the rules-based TP1; TP2/TP3 stay rule-derived
        return TradeSignal(
            entry_price=params.entry,
            stop_loss=params.stop_loss,
            tp1=tp.first_target,
            tp2=params.tp2,
            tp3=params.tp3,
            position_size=params.position_size,
            account_risk=params.risk_amount,
            reason=sr.thesis[:500],
            timestamp=int(time.time()),
            confidence=min(sr.confidence, 0.9),
        )

    # -- helpers ---------------------------------------------------------
    def _relevant_setups(self, feature_pack: Dict[str, Any]) -> List[str]:
        tags: List[str] = []
        ctx = feature_pack.get("lens_context", {})
        session = ctx.get("session")
        if session and session != "off-hours":
            tags.append(session)
        flow_m5 = feature_pack.get("lens_flow", {}).get("m5", {})
        if flow_m5.get("volume_spike"):
            tags.append("spike")
        struct_m5 = feature_pack.get("lens_structure", {}).get("m5", {})
        if struct_m5.get("last_bos") == "bull":
            tags.append("bos")
        return self.memory.relevant_setups(tags=tags or None, max_results=3)
