"""Data shapes for the discretionary Trade Copilot.

The operator inputs an intended trade (TradeProposal). The copilot returns a
CopilotVerdict (discipline result + recommended size + coaching). Closed trades
become TradeRecords; per-setup SetupStats accumulate and drive the learning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

Direction = Literal["long", "short"]


@dataclass
class TradeProposal:
    """What the operator intends to do — their discretionary trade."""
    instrument: str
    direction: Direction
    entry: float
    stop: float
    target: float
    setup: str                      # operator's setup name, e.g. "htf-retracement-short"
    thesis: str                     # why — the trade story
    kill_thesis: str                # the single observable that voids it
    conviction: int = 3             # 1-5, operator's own conviction
    session: str = "unknown"        # asia/london/ny/overlap/off
    notes: str = ""

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.stop)
        reward = abs(self.target - self.entry)
        return reward / risk if risk > 0 else 0.0

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)


@dataclass
class DisciplineCheck:
    name: str
    passed: bool
    severity: Literal["block", "warn", "info"]
    message: str


@dataclass
class CopilotVerdict:
    proposal: TradeProposal
    decision: Literal["APPROVE", "APPROVE_WITH_WARNINGS", "BLOCK"]
    checks: List[DisciplineCheck]
    recommended_risk_pct: float     # % of equity to risk on this trade
    recommended_size: float         # units, given the account + risk
    sizing_rationale: str
    coaching: List[str] = field(default_factory=list)

    @property
    def blockers(self) -> List[DisciplineCheck]:
        return [c for c in self.checks if c.severity == "block" and not c.passed]

    @property
    def warnings(self) -> List[DisciplineCheck]:
        return [c for c in self.checks if c.severity == "warn" and not c.passed]


@dataclass
class TradeRecord:
    """A logged trade (proposed, and later closed)."""
    trade_id: str
    instrument: str
    direction: Direction
    entry: float
    stop: float
    target: float
    setup: str
    thesis: str
    kill_thesis: str
    conviction: int
    session: str
    risk_pct: float
    size: float
    opened_at: int                  # ms epoch
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    exit_price: Optional[float] = None
    closed_at: Optional[int] = None
    r_multiple: Optional[float] = None
    pnl_account: Optional[float] = None
    exit_reason: str = ""            # "TP" | "SL" | "manual" | "kill_thesis" | ...
    notes: str = ""


@dataclass
class SetupStats:
    """Accumulated performance for one setup — the learning substrate."""
    setup: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    sum_r: float = 0.0
    sum_win_r: float = 0.0
    sum_loss_r: float = 0.0

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return self.wins / decided if decided else 0.0

    @property
    def expectancy_r(self) -> float:
        return self.sum_r / self.n if self.n else 0.0

    @property
    def avg_win_r(self) -> float:
        return self.sum_win_r / self.wins if self.wins else 0.0

    @property
    def avg_loss_r(self) -> float:
        # negative number
        return self.sum_loss_r / self.losses if self.losses else 0.0
