"""LLM-driven signal generator.

Composes the four-lens feature pack, fetches relevant memory, calls the
Atlas brain, and converts a TAKE SITUATION REPORT into a deterministic
TradeSignal via the existing RiskCalculator.
"""
from __future__ import annotations

import logging
import re
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


# Doctrine enforcement constants — see prompts/ATLAS_MANDATE.md §V (Laws).
MIN_KILL_THESIS_LEN = 40         # Law 3: vague kill thesis → reject
MIN_RR_MULTIPLE = 3.0            # Law 4
MAX_CONFIDENCE = 0.9             # Law 6
CONFIDENCE_RECONCILE_TOLERANCE = 0.05   # Law 5
MIN_NONZERO_LENSES = 2           # Law 7

# Vague-language blacklist for kill_thesis. Case-insensitive substring match.
# Keep tight — false positives cost trades, false negatives cost discipline.
VAGUE_KILL_PATTERNS = (
    r"\bif\s+(it|the\s+trade|the\s+market)\s+goes?\s+against\s+me\b",
    r"\bif\s+(i\s+am|i\'m|im)\s+wrong\b",
    r"\bif\s+the\s+trend\s+reverses\b",
    r"\bif\s+it\s+(doesn\'?t|does\s+not)\s+work\s+out\b",
    r"\bgut\s+feel\b",
    r"\bvibes?\b",
)
_VAGUE_KILL_RE = re.compile("|".join(VAGUE_KILL_PATTERNS), re.IGNORECASE)


def validate_take(sr: SituationReport, has_open_position: bool = False) -> Optional[str]:
    """Doctrine chokepoint. Returns None if SR can become a trade; else reason str.

    Closes laws 2, 3, 4, 5, 6, 7, 8, and 11 in one place. Keep this in sync
    with prompts/ATLAS_MANDATE.md §V — the mandate is the source of truth.
    """
    tp = sr.trade_proposal
    if tp.decision != "TAKE":
        return None  # caller handles SKIP / NO_TRADE upstream
    if has_open_position:
        return "Law 11: another position is already open on this instrument"
    if sr.data_quality != "FRESH":
        return f"Law 8: data_quality={sr.data_quality}"
    if sr.trapped_party is None:
        return "Law 2: no named trapped_party"
    if not sr.kill_thesis or len(sr.kill_thesis.strip()) < MIN_KILL_THESIS_LEN:
        return "Law 3: kill_thesis too short / vague"
    if _VAGUE_KILL_RE.search(sr.kill_thesis):
        return "Law 3: kill_thesis matches vague-language blacklist"
    if tp.rr_minimum < MIN_RR_MULTIPLE:
        return f"Law 4: rr_minimum={tp.rr_minimum} < {MIN_RR_MULTIPLE}"
    if sr.confidence > MAX_CONFIDENCE:
        return f"Law 6: confidence={sr.confidence} > {MAX_CONFIDENCE}"
    cb = sr.confidence_breakdown
    cb_sum = cb.flow + cb.structure + cb.context + cb.intent
    if abs(cb_sum - sr.confidence) > CONFIDENCE_RECONCILE_TOLERANCE:
        return (
            f"Law 5: confidence ({sr.confidence}) and breakdown sum "
            f"({cb_sum:.3f}) disagree by > {CONFIDENCE_RECONCILE_TOLERANCE}"
        )
    nonzero_lenses = sum(
        1 for v in (cb.flow, cb.structure, cb.context, cb.intent) if v > 0
    )
    if nonzero_lenses < MIN_NONZERO_LENSES:
        return f"Law 7: only {nonzero_lenses} non-zero lens(es)"
    if tp.entry_zone is None or tp.first_target is None:
        return "Schema: TAKE requires entry_zone and first_target"
    return None


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
        has_open_position: bool = False,
    ) -> Optional[TradeSignal]:
        """Convert a TAKE SR into a deterministic TradeSignal.

        Runs `validate_take` first — closes 8 doctrine laws at this single
        chokepoint. The brain provides the strategist's view (entry zone,
        first target, kill thesis). The risk calc enforces the discipline:
        SL distance, position size, R:R-derived TP2/TP3.
        """
        if sr.trade_proposal.decision != "TAKE":
            return None
        rejection = validate_take(sr, has_open_position=has_open_position)
        if rejection is not None:
            logger.info("TAKE rejected: %s", rejection)
            return None
        tp = sr.trade_proposal
        # validate_take guarantees these are non-None for TAKE
        assert tp.entry_zone is not None and tp.first_target is not None
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
            confidence=min(sr.confidence, MAX_CONFIDENCE),
            direction=tp.direction if tp.direction in ("long", "short") else "long",
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
