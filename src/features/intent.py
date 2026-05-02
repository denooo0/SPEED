"""Lens 4 — Intent.

Funding rate skew, OI delta. Bybit funding/OI fetches will feed this; pure
function for testability and graceful absence of data.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass
class IntentFeatures:
    funding_rate: Optional[float] = None
    funding_skew: Optional[str] = None  # "long-crowded" | "short-crowded" | "neutral"
    open_interest: Optional[float] = None
    oi_delta_1h: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def classify_funding_skew(rate: Optional[float]) -> Optional[str]:
    if rate is None:
        return None
    if rate > 1e-4:
        return "long-crowded"
    if rate < -1e-4:
        return "short-crowded"
    return "neutral"


def extract(
    funding_rate: Optional[float] = None,
    open_interest: Optional[float] = None,
    oi_1h_ago: Optional[float] = None,
) -> IntentFeatures:
    delta: Optional[float] = None
    if open_interest is not None and oi_1h_ago is not None:
        delta = open_interest - oi_1h_ago
    return IntentFeatures(
        funding_rate=funding_rate,
        funding_skew=classify_funding_skew(funding_rate),
        open_interest=open_interest,
        oi_delta_1h=delta,
    )
