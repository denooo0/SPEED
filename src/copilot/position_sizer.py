"""Position sizing from the operator's OWN historical edge.

Fractional-Kelly on per-setup stats. The learning: until a setup has enough
closed trades, size conservatively; as evidence accumulates, size toward the
Kelly-optimal fraction of the operator's measured edge — but always FRACTIONAL
(default 0.25x Kelly) and hard-capped. Losing setups get de-sized automatically.

Research-grounded: real systematic traders use 0.2-0.5 of full Kelly (full
Kelly's expected max drawdown approaches 100%; half-Kelly keeps ~75% of growth
at ~25% of the variance). See research/atlas_research_synthesis (Kelly nuggets).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.copilot.schema import SetupStats


@dataclass
class SizerConfig:
    kelly_fraction: float = 0.25        # fraction of full Kelly to actually use
    max_risk_pct: float = 2.0           # hard cap per trade
    min_risk_pct: float = 0.25          # floor for a live trade
    default_risk_pct: float = 0.5       # used before enough data
    min_trades_for_kelly: int = 20      # need this many closed trades to trust Kelly
    conviction_scale: bool = True       # nudge size by operator conviction (1-5)


def full_kelly_fraction(stats: SetupStats) -> float:
    """Full-Kelly bet fraction f* = W - (1-W)/b, where b = avg_win/|avg_loss|.

    Returns 0 if the edge is non-positive or undefined. This is the *bet*
    fraction of the Kelly formula; we apply it to risk budgeting, then scale
    down by kelly_fraction.
    """
    W = stats.win_rate
    avg_win = stats.avg_win_r
    avg_loss = abs(stats.avg_loss_r)
    if avg_loss <= 0 or avg_win <= 0 or W <= 0:
        return 0.0
    b = avg_win / avg_loss
    f = W - (1.0 - W) / b
    return max(0.0, f)


def recommend_risk_pct(
    stats: SetupStats,
    conviction: int = 3,
    config: SizerConfig | None = None,
) -> tuple[float, str]:
    """Return (risk_pct, rationale) for a trade of this setup."""
    cfg = config or SizerConfig()

    if stats.n < cfg.min_trades_for_kelly:
        risk = cfg.default_risk_pct
        rationale = (
            f"Only {stats.n} closed '{stats.setup}' trades logged "
            f"(need {cfg.min_trades_for_kelly} to trust Kelly). "
            f"Using conservative default {risk:.2f}%."
        )
    else:
        f_full = full_kelly_fraction(stats)
        if f_full <= 0:
            risk = cfg.min_risk_pct
            rationale = (
                f"'{stats.setup}' has non-positive measured edge over {stats.n} "
                f"trades (win {stats.win_rate:.0%}, exp {stats.expectancy_r:+.2f}R). "
                f"Kelly says don't bet — flooring at {risk:.2f}% (consider skipping)."
            )
        else:
            # Interpret the scaled Kelly fraction as a risk budget %, then cap.
            risk = f_full * cfg.kelly_fraction * 100.0
            risk = max(cfg.min_risk_pct, min(cfg.max_risk_pct, risk))
            rationale = (
                f"'{stats.setup}': win {stats.win_rate:.0%}, "
                f"avg win {stats.avg_win_r:+.2f}R / avg loss {stats.avg_loss_r:+.2f}R "
                f"over {stats.n} trades → full-Kelly {f_full:.2f}, "
                f"{cfg.kelly_fraction:g}x-Kelly risk {risk:.2f}%."
            )

    # Conviction nudge: scale within [0.7, 1.15] around the base for 1..5.
    if cfg.conviction_scale:
        conv = max(1, min(5, conviction))
        scale = 0.7 + (conv - 1) * (0.45 / 4.0)  # 1->0.70, 3->1.0(ish), 5->1.15
        risk = max(cfg.min_risk_pct, min(cfg.max_risk_pct, risk * scale))
        rationale += f" Conviction {conv}/5 → ×{scale:.2f}."

    return round(risk, 3), rationale


def size_from_risk(risk_pct: float, account_equity: float, risk_per_unit: float) -> float:
    """Units to trade so that a stop-out loses `risk_pct` of equity."""
    if risk_per_unit <= 0:
        return 0.0
    risk_amount = account_equity * (risk_pct / 100.0)
    return risk_amount / risk_per_unit
