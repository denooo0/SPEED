"""Detects spike-low / consolidation / bounce patterns on a single timeframe."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def price_diff_pips(price_a: float, price_b: float) -> float:
    """Return the relative move between two prices in basis points (the spec's 'pips')."""
    if price_b == 0:
        return 0.0
    return abs(price_a - price_b) / price_b * 10_000


@dataclass
class SpikeState:
    detected: bool = False
    spike_price: Optional[float] = None
    spike_time: Optional[int] = None
    spike_ad: Optional[float] = None
    spike_index: Optional[int] = None  # absolute candle index when spike was set


class SpikeDetector:
    """Identify panic spike low → consolidation → bounce on M5."""

    def __init__(
        self,
        min_spike_pips: float = 80,
        min_spike_candles: int = 5,
        consolidation_candles: int = 5,
        consolidation_max_range_pips: float = 20,
        spike_validity_candles: int = 30,
    ) -> None:
        self.min_spike_pips = float(min_spike_pips)
        self.min_spike_candles = int(min_spike_candles)
        self.consolidation_candles = int(consolidation_candles)
        self.consolidation_max_range_pips = float(consolidation_max_range_pips)
        self.spike_validity_candles = int(spike_validity_candles)
        self.state = SpikeState()
        self._candles_seen = 0

    def reset(self) -> None:
        self.state = SpikeState()

    def update(
        self,
        candles: List[Dict[str, float]],
        indicators: Dict[str, Any],
    ) -> Dict[str, bool]:
        """Update detector with the latest candle history. Returns flags."""
        self._candles_seen = len(candles)
        spike = self._detect_spike(candles, indicators)
        if self.state.detected and self.state.spike_index is not None:
            if (self._candles_seen - self.state.spike_index) > self.spike_validity_candles:
                logger.debug("Spike at index %d expired", self.state.spike_index)
                self.reset()

        consolidating = self.is_consolidating(candles)
        bouncing = self.is_bouncing(candles, indicators)
        return {"spike": spike, "consolidating": consolidating, "bouncing": bouncing}

    def _detect_spike(
        self,
        candles: List[Dict[str, float]],
        indicators: Dict[str, Any],
    ) -> bool:
        if len(candles) < self.min_spike_candles:
            return False
        recent = candles[-self.min_spike_candles :]
        highest = max(c["high"] for c in recent)
        lowest = min(c["low"] for c in recent)
        drop_pips = price_diff_pips(highest, lowest)
        if drop_pips < self.min_spike_pips:
            return False
        if not indicators.get("ad_extreme"):
            return False
        # Spike already locked at same low? skip
        if self.state.detected and self.state.spike_price == lowest:
            return False
        self.state = SpikeState(
            detected=True,
            spike_price=lowest,
            spike_time=int(candles[-1]["timestamp"]),
            spike_ad=indicators.get("ad"),
            spike_index=self._candles_seen,
        )
        logger.info(
            "SPIKE DETECTED low=%.2f drop=%.1fbp ad=%s",
            lowest,
            drop_pips,
            indicators.get("ad"),
        )
        return True

    def is_consolidating(self, candles: List[Dict[str, float]]) -> bool:
        if not self.state.detected or len(candles) < self.consolidation_candles:
            return False
        recent = candles[-self.consolidation_candles :]
        highest = max(c["high"] for c in recent)
        lowest = min(c["low"] for c in recent)
        rng = price_diff_pips(highest, lowest)
        return rng < self.consolidation_max_range_pips

    def is_bouncing(
        self,
        candles: List[Dict[str, float]],
        indicators: Dict[str, Any],
    ) -> bool:
        if not self.state.detected:
            return False
        if not self.is_consolidating(candles):
            return False
        if not indicators.get("ad_turning_up"):
            return False
        ma_fast = indicators.get("ma_fast")
        if ma_fast is None:
            return False
        if candles[-1]["close"] <= ma_fast:
            return False
        logger.info("BOUNCE CONFIRMED close=%.2f ma_fast=%.2f", candles[-1]["close"], ma_fast)
        return True
