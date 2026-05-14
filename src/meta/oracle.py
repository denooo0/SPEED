"""Backtest oracle for meta-loop validation.

A ProposedChange is applied to a SHADOW configuration (not live), the
backtest harness replays held-out historical data with the shadow config,
and the oracle returns a structured result. The change deploys only if
the result passes the gates below.

IMPLEMENTATION BLOCKED: requires the v9 backtest harness (see
plan/meta_learning_loop.md §V Phase M-0). Skeleton only here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from src.meta.proposer import ProposedChange


@dataclass
class OracleResult:
    change_id: str
    passed: bool
    reason: str                       # one-line summary of pass/fail
    metrics: Dict[str, float]         # sharpe, win_rate, avg_r, max_dd, dsr, pbo, ruin_prob
    baseline_metrics: Dict[str, float]
    regimes_tested: List[str]         # e.g. ["trending_bull_2024Q1", "sideways_2024Q3"]
    regimes_failed: List[str]


# Gate constants — keep in sync with plan/meta_learning_loop.md §IV
SHARPE_IMPROVEMENT_MIN = 0.10
PBO_MAX = 0.20
DSR_MIN = 0.95


class BacktestOracle:
    """Validates a ProposedChange against held-out historical data.

    The oracle must be DETERMINISTIC and not modifiable by the meta-loop
    itself (Tier D forbidden — see plan §III).
    """

    def __init__(self, backtest_harness: Any) -> None:
        # backtest_harness will be the v9 module once it exists.
        self.harness = backtest_harness

    def validate(self, change: ProposedChange) -> OracleResult:
        """Apply change to a shadow config, run backtest, return result.

        Pass criteria (all required):
          - Sharpe improves by >= SHARPE_IMPROVEMENT_MIN vs baseline
          - PBO <= PBO_MAX
          - DSR >= DSR_MIN
          - Ruin probability does not increase vs baseline
          - Passes in ALL regime archives tested (no per-regime opt-outs)
        """
        raise NotImplementedError
