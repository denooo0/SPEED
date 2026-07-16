"""Structured output schema for vision chart analysis → limit order."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel

Direction = Literal["long", "short", "none"]


class KeyLevel(BaseModel):
    price: float
    role: str  # "support" | "resistance" | "prior high" | "FVG" | "MA" | ...


class ChartAnalysis(BaseModel):
    """What the vision LLM extracts from a chart image."""
    instrument: str
    timeframe: str                       # as read from the chart (e.g. "H1", "M30")
    regime: str                          # uptrend / downtrend / range / distribution / ...
    setup_present: bool
    direction: Direction
    # Limit order (only meaningful when setup_present and direction != none)
    entry_limit: Optional[float] = None
    stop: Optional[float] = None
    targets: List[float] = []
    thesis: str = ""                     # why — the trade story
    kill_thesis: str = ""                # the single observable that voids it
    trapped_party: str = ""              # who is caught (mandate Law 2)
    confidence: float = 0.0              # 0..1
    key_levels: List[KeyLevel] = []
    notes: str = ""

    @property
    def rr(self) -> float:
        if self.entry_limit is None or self.stop is None or not self.targets:
            return 0.0
        risk = abs(self.entry_limit - self.stop)
        reward = abs(self.targets[0] - self.entry_limit)
        return reward / risk if risk > 0 else 0.0
