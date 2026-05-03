"""Lens 4 — Intent.

Funding rate skew, OI delta, and CFTC COT positioning. Bybit funding/OI
fetches feed the perp side; CFTC COT (weekly) feeds the institutional side.
Pure function for testability and graceful absence of data.
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
    # COT (weekly, from CFTC)
    cot_report_date: Optional[str] = None
    commercial_net: Optional[float] = None
    commercial_bias: Optional[str] = None       # "long" | "short" | "neutral"
    managed_money_net: Optional[float] = None
    managed_money_bias: Optional[str] = None
    cot_alignment: Optional[str] = None         # "commercials_vs_specs" | "aligned" | None

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


def classify_cot_alignment(commercial_bias: Optional[str], mm_bias: Optional[str]) -> Optional[str]:
    """Smart money vs dumb money positioning."""
    if commercial_bias in (None, "neutral") or mm_bias in (None, "neutral"):
        return None
    if commercial_bias == mm_bias:
        return "aligned"
    return "commercials_vs_specs"


def extract(
    funding_rate: Optional[float] = None,
    open_interest: Optional[float] = None,
    oi_1h_ago: Optional[float] = None,
    cot_snapshot: Optional[Any] = None,
) -> IntentFeatures:
    delta: Optional[float] = None
    if open_interest is not None and oi_1h_ago is not None:
        delta = open_interest - oi_1h_ago

    out = IntentFeatures(
        funding_rate=funding_rate,
        funding_skew=classify_funding_skew(funding_rate),
        open_interest=open_interest,
        oi_delta_1h=delta,
    )

    if cot_snapshot is not None:
        out.cot_report_date = getattr(cot_snapshot, "report_date", None)
        out.commercial_net = getattr(cot_snapshot, "commercial_net", None)
        out.commercial_bias = getattr(cot_snapshot, "commercial_bias", None)
        out.managed_money_net = getattr(cot_snapshot, "managed_money_net", None)
        out.managed_money_bias = getattr(cot_snapshot, "managed_money_bias", None)
        out.cot_alignment = classify_cot_alignment(
            out.commercial_bias, out.managed_money_bias
        )
    return out
