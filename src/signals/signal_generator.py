"""Combine multi-timeframe context + spike pattern into trade signals."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from src.patterns.spike_detector import SpikeDetector
from src.risk.risk_calculator import RiskCalculator

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    position_size: float
    account_risk: float
    reason: str
    timestamp: int
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SignalGenerator:
    """Multi-timeframe gate: M5 → M15 → M30 → H1."""

    def __init__(self, config: Dict[str, Any], risk_calc: RiskCalculator) -> None:
        self.config = config
        self.risk_calc = risk_calc
        self.last_signal_time = 0
        self.min_signal_interval = int(
            config.get("SIGNAL", {}).get("min_signal_interval_seconds", 3600)
        )

    def generate(
        self,
        candles_m5: List[Dict[str, float]],
        candles_m15: List[Dict[str, float]],
        candles_m30: List[Dict[str, float]],
        candles_h1: List[Dict[str, float]],
        ind_m5: Dict[str, Any],
        ind_m15: Dict[str, Any],
        ind_m30: Dict[str, Any],
        ind_h1: Dict[str, Any],
        spike_detector: SpikeDetector,
        account_balance: float,
    ) -> Optional[TradeSignal]:
        now = int(time.time())
        if (now - self.last_signal_time) < self.min_signal_interval:
            return None

        spike_detector.update(candles_m5, ind_m5)

        if not spike_detector.state.detected:
            return None
        if not spike_detector.is_bouncing(candles_m5, ind_m5):
            return None

        if not self._m15_confirms(ind_m15):
            logger.debug("M5 bounce but M15 not confirming")
            return None
        if not self._m30_aligns(candles_m30, ind_m30):
            logger.debug("M5+M15 ok but M30 not aligned")
            return None
        if self._h1_breaking_down(candles_h1, ind_h1):
            logger.warning("H1 breaking down — suppressing signal")
            return None

        signal = self._build_signal(spike_detector, candles_m5, ind_m5, account_balance, now)
        if signal is None:
            return None

        self.last_signal_time = now
        spike_detector.reset()
        return signal

    # -- gating helpers --------------------------------------------------
    @staticmethod
    def _m15_confirms(ind: Dict[str, Any]) -> bool:
        if ind.get("ma_fast") is None:
            return False
        ad = ind.get("ad")
        if ad is None:
            return False
        if ad < 0 and not ind.get("ad_improving"):
            return False
        return True

    @staticmethod
    def _m30_aligns(candles: List[Dict[str, float]], ind: Dict[str, Any]) -> bool:
        if not candles:
            return False
        ma = ind.get("ma_fast")
        if ma is None:
            return False
        return candles[-1]["close"] >= ma * 0.995

    @staticmethod
    def _h1_breaking_down(candles: List[Dict[str, float]], ind: Dict[str, Any]) -> bool:
        if not candles or len(candles) < 2:
            return False
        ma = ind.get("ma_slow")
        if ma is None:
            return False
        if not all(c["close"] < ma for c in candles[-2:]):
            return False
        rising = ind.get("ma_slow_rising")
        if rising is False:
            return True
        return False

    def _build_signal(
        self,
        spike_detector: SpikeDetector,
        candles_m5: List[Dict[str, float]],
        ind_m5: Dict[str, Any],
        account_balance: float,
        now: int,
    ) -> Optional[TradeSignal]:
        spike_price = spike_detector.state.spike_price
        if spike_price is None:
            return None
        # Entry just above spike low at consolidation breakout level
        latest_close = candles_m5[-1]["close"]
        entry = max(latest_close, spike_price * 1.001)

        params = self.risk_calc.build(entry=entry, account_balance=account_balance)
        if params.position_size <= 0:
            logger.warning("Computed zero position size — skipping signal")
            return None

        confidence = self._confidence(ind_m5)
        reason = (
            f"Spike low {spike_price:.2f} + bounce confirmed; "
            f"AD={ind_m5.get('ad'):.0f}; vol×{ind_m5.get('volume_ratio'):.2f}"
        )
        return TradeSignal(
            entry_price=params.entry,
            stop_loss=params.stop_loss,
            tp1=params.tp1,
            tp2=params.tp2,
            tp3=params.tp3,
            position_size=params.position_size,
            account_risk=params.risk_amount,
            reason=reason,
            timestamp=now,
            confidence=confidence,
        )

    @staticmethod
    def _confidence(ind_m5: Dict[str, Any]) -> float:
        score = 0.6
        if ind_m5.get("volume_spike"):
            score += 0.1
        if ind_m5.get("ad_improving"):
            score += 0.1
        if ind_m5.get("ma_fast_rising"):
            score += 0.05
        return min(score, 0.95)
