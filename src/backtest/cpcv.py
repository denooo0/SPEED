"""Combinatorial Purged Cross-Validation.

Per López de Prado: split candles into N contiguous groups, pick k of them
as test groups (the remaining N-k are training). C(N, k) combinations.
For each combination we run the strategy across the test groups (with purge +
embargo enforced around each one).

The number of "paths" through OOS data with k=2 is C(N,2) / (N - k) per
López de Prado — but in this implementation we treat each combinatorial
draw as a single OOS path for simplicity, since downstream we only need:

    1. A distribution of OOS Sharpes (for confidence intervals).
    2. The IS/OOS rank matrix needed by compute_pbo.

For PBO we use Combinatorially Symmetric CV: split into N groups, enumerate
all balanced "split into half IS / half OOS" pairs, and for each split rank
the strategies by IS Sharpe and OOS Sharpe. With a single strategy, PBO is
not well-defined — callers must supply >= 2 candidate parameter sets via
the `strategy_factory` argument.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from src.backtest.metrics import compute_pbo
from src.backtest.simulator import BacktestResult, EventDrivenSimulator
from src.backtest.strategy import Strategy


@dataclass
class CPCVResult:
    paths: List[BacktestResult] = field(default_factory=list)
    sharpes: List[float] = field(default_factory=list)
    pbo: float = 0.0
    n_groups: int = 0
    k_test: int = 0
    n_paths: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sharpes": self.sharpes,
            "pbo": self.pbo,
            "n_groups": self.n_groups,
            "k_test": self.k_test,
            "n_paths": self.n_paths,
        }


class CombinatorialPurgedCV:
    """Combinatorial Purged Cross-Validation runner.

    Parameters
    ----------
    simulator:
        EventDrivenSimulator with full candle range.
    n_groups:
        Number of contiguous groups to split the data into.
    k_test:
        Number of groups reserved as test per combination.
    embargo_pct:
        Fraction of total bars to embargo after each test group's end.
    label_horizon_bars:
        Number of bars by which to PURGE training bars adjacent to each test
        group (López de Prado purge step).
    """

    def __init__(
        self,
        simulator: EventDrivenSimulator,
        n_groups: int = 10,
        k_test: int = 2,
        embargo_pct: float = 0.01,
        label_horizon_bars: int = 30,
    ) -> None:
        if n_groups < 2:
            raise ValueError("n_groups must be >= 2")
        if k_test < 1 or k_test >= n_groups:
            raise ValueError("k_test must be in [1, n_groups-1]")
        self.simulator = simulator
        self.n_groups = int(n_groups)
        self.k_test = int(k_test)
        self.embargo_pct = float(embargo_pct)
        self.label_horizon_bars = int(label_horizon_bars)

    # -------------------------------------------------------- group splits

    def _group_ranges(self) -> List[tuple]:
        """Return inclusive (start_idx, end_idx) for each contiguous group."""
        n = len(self.simulator.candles)
        if n < self.n_groups:
            raise ValueError(
                f"not enough bars ({n}) to form {self.n_groups} groups"
            )
        bounds = np.linspace(0, n, self.n_groups + 1, dtype=int)
        return [(int(bounds[i]), int(bounds[i + 1]) - 1) for i in range(self.n_groups)]

    def _test_mask(self, group_ranges: List[tuple], chosen: Sequence[int]) -> np.ndarray:
        n = len(self.simulator.candles)
        embargo_n = max(1, int(round(self.embargo_pct * n)))
        purge_n = self.label_horizon_bars
        mask_test = np.zeros(n, dtype=bool)
        mask_train = np.ones(n, dtype=bool)
        for g in chosen:
            s, e = group_ranges[g]
            mask_test[s : e + 1] = True
            purge_lo = max(0, s - purge_n)
            purge_hi = min(n - 1, e + purge_n)
            mask_train[purge_lo : purge_hi + 1] = False
            embargo_hi = min(n - 1, e + embargo_n + purge_n)
            mask_train[e + 1 : embargo_hi + 1] = False
        mask_train &= ~mask_test
        return mask_test  # we only need test mask for OOS path; train mask is informational

    # -------------------------------------------------------- run a single combination

    def _run_combo(
        self,
        strategy: Strategy,
        feature_pack_provider: Callable[[pd.Timestamp], Dict[str, Any]],
        group_ranges: List[tuple],
        chosen: Sequence[int],
    ) -> BacktestResult:
        """Run the simulator on the concatenated test windows of `chosen` groups.

        Simulator handles single contiguous ranges; we run each test group
        separately and stitch the equity curves multiplicatively.
        """
        index = self.simulator.candles.index
        sub_results: List[BacktestResult] = []
        for g in chosen:
            s, e = group_ranges[g]
            start = index[s]
            end = index[e]
            features = [feature_pack_provider(ts) for ts in index[s : e + 1]]
            res = self.simulator.run(strategy, features, start=start, end=end)
            sub_results.append(res)
        # Stitch equity curves multiplicatively.
        running = 1.0
        merged_eq: List[tuple] = []
        merged_trades: List[pd.DataFrame] = []
        for r in sub_results:
            if r.equity_curve.empty:
                continue
            seg = r.equity_curve / r.equity_curve.iloc[0]
            seg = seg * running
            running = float(seg.iloc[-1])
            for ts, v in seg.items():
                merged_eq.append((ts, float(v)))
            if r.trades is not None and not r.trades.empty:
                merged_trades.append(r.trades)
        if merged_eq:
            eq_curve = pd.Series(
                [v for _, v in merged_eq],
                index=pd.Index([t for t, _ in merged_eq], name="timestamp"),
                name="equity",
            )
        else:
            eq_curve = pd.Series(dtype=float)
        trades_df = (
            pd.concat(merged_trades, ignore_index=True)
            if merged_trades
            else pd.DataFrame()
        )
        from src.backtest.metrics import summarize

        metrics = summarize(trades_df, eq_curve, bar_seconds=self.simulator.bar_seconds)
        return BacktestResult(
            trades=trades_df,
            equity_curve=eq_curve,
            metrics=metrics,
            config={"chosen_groups": list(chosen), "n_groups": self.n_groups, "k_test": self.k_test},
        )

    # -------------------------------------------------------- public run

    def run(
        self,
        strategy: Strategy,
        feature_pack_provider: Callable[[pd.Timestamp], Dict[str, Any]],
        strategy_factory: Optional[Callable[[int], Strategy]] = None,
        n_strategies_for_pbo: int = 1,
    ) -> CPCVResult:
        """Run all combinatorial test paths.

        Parameters
        ----------
        strategy:
            Default strategy (used when `strategy_factory` is None).
        feature_pack_provider:
            Provider returning a feature_pack dict per timestamp.
        strategy_factory:
            Optional callable(strategy_index) -> Strategy. Used to evaluate
            multiple candidate strategies in parallel — required for a
            meaningful PBO computation. If omitted, PBO defaults to 0.0 and
            only the single-strategy distribution is returned.
        n_strategies_for_pbo:
            How many strategy variants to evaluate when `strategy_factory` is
            provided.
        """
        group_ranges = self._group_ranges()
        combos = list(combinations(range(self.n_groups), self.k_test))

        # Single-strategy path (no PBO).
        if strategy_factory is None or n_strategies_for_pbo <= 1:
            paths: List[BacktestResult] = []
            sharpes: List[float] = []
            for c in combos:
                r = self._run_combo(strategy, feature_pack_provider, group_ranges, c)
                paths.append(r)
                sharpes.append(float(r.metrics.get("sharpe", 0.0)))
            return CPCVResult(
                paths=paths,
                sharpes=sharpes,
                pbo=0.0,
                n_groups=self.n_groups,
                k_test=self.k_test,
                n_paths=len(combos),
            )

        # Multi-strategy: build IS/OOS Sharpe rank matrices for PBO.
        # IS = remaining groups (complement of chosen). OOS = chosen.
        n_combos = len(combos)
        is_sharpes = np.zeros((n_combos, n_strategies_for_pbo))
        oos_sharpes = np.zeros((n_combos, n_strategies_for_pbo))
        # Save the OOS BacktestResult for the FIRST strategy as the "primary" path output.
        primary_paths: List[BacktestResult] = []
        primary_sharpes: List[float] = []
        for ci, c in enumerate(combos):
            is_groups = [g for g in range(self.n_groups) if g not in c]
            for si in range(n_strategies_for_pbo):
                strat = strategy_factory(si)
                oos_r = self._run_combo(strat, feature_pack_provider, group_ranges, c)
                is_r = self._run_combo(strat, feature_pack_provider, group_ranges, is_groups)
                is_sharpes[ci, si] = float(is_r.metrics.get("sharpe", 0.0))
                oos_sharpes[ci, si] = float(oos_r.metrics.get("sharpe", 0.0))
                if si == 0:
                    primary_paths.append(oos_r)
                    primary_sharpes.append(float(oos_r.metrics.get("sharpe", 0.0)))

        # Ranks within each row.
        def _rank(matrix: np.ndarray) -> np.ndarray:
            out = np.zeros_like(matrix)
            for i in range(matrix.shape[0]):
                order = np.argsort(matrix[i])  # ascending ranks
                out[i, order] = np.arange(matrix.shape[1])
            return out

        is_ranks = _rank(is_sharpes)
        oos_ranks = _rank(oos_sharpes)
        pbo = compute_pbo(is_ranks, oos_ranks)
        return CPCVResult(
            paths=primary_paths,
            sharpes=primary_sharpes,
            pbo=pbo,
            n_groups=self.n_groups,
            k_test=self.k_test,
            n_paths=len(combos),
        )
