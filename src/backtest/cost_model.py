"""Realistic execution cost model — Bybit XAUUSD perp.

Components per round-trip:
  - Taker/maker fee (entry + exit)
  - Spread crossing (half-spread on entry, half-spread on exit)
  - Slippage (worse fill than mid)
  - Funding drag (proportional to hold time, sign depends on side)

All numbers are basis points (bps). 1bp = 0.0001 = 0.01%.

Per research findings (see plan/v9_generational_wealth.md and edge_generation_workflow.md):
  ~11bp round-trip on Bybit XAUUSD perp is realistic in normal conditions;
  spread + slippage blow out in news windows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


OrderType = Literal["taker", "maker"]
Side = Literal["long", "short"]


@dataclass
class CostModel:
    """Bybit-derived cost defaults. All fields in basis points unless stated."""

    spread_bps_normal: float = 4.0
    spread_bps_news: float = 25.0
    slippage_bps_normal: float = 2.0
    slippage_bps_news: float = 8.0
    fee_bps_taker: float = 5.5
    fee_bps_maker: float = 2.0
    funding_bps_per_8h: float = 1.5
    latency_seconds: float = 1.5

    # ---------------- public API ----------------

    def entry_cost_bps(
        self,
        side: Side,
        news_window: bool,
        order_type: OrderType = "taker",
    ) -> float:
        """Total entry-side cost in bps.

        Charged components on entry:
          - half-spread (we cross half the bid/ask to get filled)
          - slippage (worse fill than the level we wanted)
          - fee (taker or maker)
        Side does not change the magnitude because we always *pay* on entry.
        """
        _ = side  # side parameter retained for future asymmetric LP models
        spread = self.spread_bps_news if news_window else self.spread_bps_normal
        slip = self.slippage_bps_news if news_window else self.slippage_bps_normal
        fee = self.fee_bps_taker if order_type == "taker" else self.fee_bps_maker
        return 0.5 * spread + slip + fee

    def exit_cost_bps(
        self,
        side: Side,
        news_window: bool,
        order_type: OrderType = "taker",
    ) -> float:
        """Symmetric exit cost. See `entry_cost_bps` notes."""
        _ = side
        spread = self.spread_bps_news if news_window else self.spread_bps_normal
        slip = self.slippage_bps_news if news_window else self.slippage_bps_normal
        fee = self.fee_bps_taker if order_type == "taker" else self.fee_bps_maker
        return 0.5 * spread + slip + fee

    def funding_drag_bps(self, hold_hours: float, side: Side) -> float:
        """Funding drag in bps over the holding period.

        Funding is charged every 8 hours on perp. We model the *average* magnitude
        as a drag on longs (positive funding paid to shorts) — the convention here
        is `funding_bps_per_8h` represents the realized cost-to-long. Shorts get
        the symmetric *benefit* (negative drag).

        Negative return values indicate a benefit (reducing total cost).
        """
        if hold_hours <= 0:
            return 0.0
        intervals = hold_hours / 8.0
        magnitude = self.funding_bps_per_8h * intervals
        return magnitude if side == "long" else -magnitude

    def round_trip_cost_bps(
        self,
        side: Side,
        news_window: bool,
        hold_hours: float = 0.0,
        order_type: OrderType = "taker",
    ) -> float:
        """Convenience: full round-trip cost incl. funding."""
        return (
            self.entry_cost_bps(side, news_window, order_type)
            + self.exit_cost_bps(side, news_window, order_type)
            + self.funding_drag_bps(hold_hours, side)
        )
