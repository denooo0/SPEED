"""Pydantic schema for the SITUATION REPORT produced by ATLAS.

Mirrors the contract in `prompts/ATLAS_MANDATE.md` §IV. The brain validates
its own output against this; downstream code consumes it as a typed object.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel


DataQuality = Literal["FRESH", "DEGRADED", "STALE"]
Decision = Literal["TAKE", "SKIP", "NO_TRADE"]
Direction = Literal["long", "short", "n/a"]
Regime = Literal[
    "accumulation",
    "markup",
    "distribution",
    "markdown",
    "reaccumulation",
    "redistribution",
    "indeterminate",
]


class TrappedParty(BaseModel):
    who: str
    level: float
    pain_bp: float


class DominantParty(BaseModel):
    who: str
    mechanism: str


class EntryZone(BaseModel):
    low: float
    high: float


class TradeProposal(BaseModel):
    decision: Decision
    direction: Direction
    entry_zone: Optional[EntryZone] = None
    invalidation: str
    first_target: Optional[float] = None
    rr_minimum: float
    size_pct: float


class ConfidenceBreakdown(BaseModel):
    flow: float
    structure: float
    context: float
    intent: float


class SituationReport(BaseModel):
    timestamp: str
    instrument: str
    data_quality: DataQuality
    regime: Regime
    evidence: List[str]
    trapped_party: Optional[TrappedParty] = None
    dominant_party: Optional[DominantParty] = None
    asymmetry: str
    thesis: str
    trade_proposal: TradeProposal
    kill_thesis: str
    confidence: float
    confidence_breakdown: ConfidenceBreakdown
    notes_to_future_atlas: str
