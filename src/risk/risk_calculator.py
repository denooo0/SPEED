"""Position sizing and SL/TP calculation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


def pips_to_price(reference_price: float, pips: float) -> float:
    """Convert basis-point 'pips' (per spec) to absolute price delta."""
    return (pips / 10_000) * reference_price


def price_distance_pips(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return abs(a - b) / b * 10_000


@dataclass
class RiskParameters:
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    position_size: float
    risk_amount: float
    risk_pips: float


class RiskCalculator:
    """Compute SL/TP and position size from config."""

    def __init__(
        self,
        account_risk_pct: float,
        sl_distance_pips: float,
        tp_distances_pips: List[float],
        max_units_per_trade: float | None = None,
    ) -> None:
        if len(tp_distances_pips) < 3:
            raise ValueError("tp_distances_pips needs at least 3 entries")
        self.account_risk_pct = float(account_risk_pct)
        self.sl_distance_pips = float(sl_distance_pips)
        self.tp_distances_pips = [float(p) for p in tp_distances_pips]
        self.max_units_per_trade = max_units_per_trade

    def build(self, entry: float, account_balance: float) -> RiskParameters:
        if entry <= 0:
            raise ValueError("entry must be positive")
        sl = entry - pips_to_price(entry, self.sl_distance_pips)
        risk_per_unit = entry - sl
        if risk_per_unit <= 0:
            raise ValueError("risk_per_unit must be positive")

        risk_amount = account_balance * (self.account_risk_pct / 100.0)
        size = risk_amount / risk_per_unit if risk_per_unit > 0 else 0.0
        if self.max_units_per_trade is not None:
            size = min(size, float(self.max_units_per_trade))

        tp1 = entry + pips_to_price(entry, self.tp_distances_pips[0])
        tp2 = entry + pips_to_price(entry, self.tp_distances_pips[1])
        tp3 = entry + pips_to_price(entry, self.tp_distances_pips[2])

        return RiskParameters(
            entry=entry,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            position_size=size,
            risk_amount=risk_amount,
            risk_pips=self.sl_distance_pips,
        )
