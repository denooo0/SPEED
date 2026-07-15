"""A/B backtest harness for LLM-as-filter (Path A).

Runs the same base strategy twice on the same candles:

    A. baseline  — no brain, all base orders execute
    B. filtered  — LLMFilterStrategy wraps the base strategy; brain must agree

The difference tells us whether the LLM's TAKE/SKIP judgement is adding
economic value once its per-call cost is subtracted.

Definitions used here:

    filter_alpha   = filtered.total_return - baseline.total_return
    llm_cost_usd   = tracker.estimated_usd()
    net_alpha_usd  = (filter_alpha * initial_equity_usd) - llm_cost_usd

The last one is the honest number: an LLM filter that lifts return by +1% on
$500 of equity but costs $12 in Claude calls is a $7 loss dressed as alpha.

The base strategy MUST be deterministic — running it twice on the same
candles has to produce identical baseline results. A/B splits on random-seed
divergences will contaminate the comparison. We defensively construct a fresh
base strategy for each arm via ``strategy_factory`` (if given), otherwise we
rely on the caller to pass a stateless instance.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import pandas as pd

from src.backtest.cost_model import CostModel
from src.backtest.cost_telemetry import CostTracker
from src.backtest.llm_filter import LLMFilterStrategy
from src.backtest.simulator import BacktestResult, EventDrivenSimulator
from src.backtest.strategy import Strategy


@dataclass
class ABResult:
    """Bundle of both runs plus the derived economics."""

    baseline: BacktestResult
    filtered: BacktestResult
    filter_alpha: float          # filtered.total_return - baseline.total_return
    llm_cost_usd: float
    net_alpha_usd: float
    trades_filtered_out: int     # base orders the brain vetoed
    trades_approved: int
    cache_hit_rate: float
    cost_tracker: CostTracker
    filter_stats: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        return {
            "baseline_trades": self.baseline.n_trades,
            "filtered_trades": self.filtered.n_trades,
            "baseline_return": self.baseline.total_return,
            "filtered_return": self.filtered.total_return,
            "filter_alpha": self.filter_alpha,
            "trades_filtered_out": self.trades_filtered_out,
            "trades_approved": self.trades_approved,
            "cache_hit_rate": self.cache_hit_rate,
            "brain_calls": self.cost_tracker.total_calls,
            "llm_cost_usd": self.llm_cost_usd,
            "net_alpha_usd": self.net_alpha_usd,
        }


class ABBacktestHarness:
    """Run baseline + filtered arms on identical inputs.

    Parameters
    ----------
    cost_model:
        Execution cost model shared by both arms.
    feature_replayer:
        Optional replayer that provides the rich FeaturePack to the brain.
        Not used by the baseline arm.
    brain:
        The AtlasBrain (or Fake) used by the filtered arm. Baseline never
        calls it.
    memory_digest:
        Held constant across the run; passed to the brain each call.
    bar_seconds:
        Bar duration seconds — forwarded to the simulator.
    initial_equity_usd:
        Only used to scale `filter_alpha` into `net_alpha_usd`. Default 500
        matches the plan's starting capital.
    is_news_window:
        Optional predicate forwarded to both simulators for symmetry.
    cache_ttl_bars:
        LLMFilterStrategy cache TTL. Forwarded verbatim.
    """

    def __init__(
        self,
        cost_model: CostModel,
        feature_replayer: Optional[Any] = None,
        brain: Optional[Any] = None,
        memory_digest: str = "",
        bar_seconds: int = 300,
        initial_equity_usd: float = 500.0,
        is_news_window: Callable[[pd.Timestamp], bool] = lambda _ts: False,
        cache_ttl_bars: int = 100,
    ) -> None:
        self.cost_model = cost_model
        self.feature_replayer = feature_replayer
        self.brain = brain
        self.memory_digest = memory_digest
        self.bar_seconds = int(bar_seconds)
        self.initial_equity_usd = float(initial_equity_usd)
        self.is_news_window = is_news_window
        self.cache_ttl_bars = int(cache_ttl_bars)

    # ---------------------------------------------------------------- public

    def run(
        self,
        base_strategy: Strategy,
        candles: pd.DataFrame,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
        strategy_factory: Optional[Callable[[], Strategy]] = None,
    ) -> ABResult:
        """Run both arms. Returns an ABResult with the derived economics.

        If ``strategy_factory`` is supplied, we call it once per arm to get a
        fresh instance — this avoids state leakage between the two runs
        (e.g. an internal ``fired`` flag that wouldn't reset).

        ``feature_pack_iter`` for both arms is generated the same way: from
        ``feature_replayer.iter_packs(start, end)`` when available, else empty
        dicts. Both arms must see the *same* feature packs so the only
        variable is the LLM filter.
        """
        # Feature packs are shared between arms; materialise once.
        feature_packs = self._materialise_feature_packs(candles, start, end)

        baseline_strategy = strategy_factory() if strategy_factory else base_strategy
        baseline_sim = EventDrivenSimulator(
            candles=candles,
            cost_model=self.cost_model,
            is_news_window=self.is_news_window,
            bar_seconds=self.bar_seconds,
        )
        baseline_result = baseline_sim.run(
            baseline_strategy,
            list(feature_packs),
            start=start,
            end=end,
        )

        # Filtered arm — fresh base strategy so no state leaks.
        filtered_base = strategy_factory() if strategy_factory else self._clone_strategy(base_strategy)
        cost_tracker = CostTracker()
        wrapper = LLMFilterStrategy(
            base_strategy=filtered_base,
            brain=self.brain,
            feature_replayer=self.feature_replayer,
            memory_digest=self.memory_digest,
            cost_tracker=cost_tracker,
            cache_ttl_bars=self.cache_ttl_bars,
        )
        filtered_sim = EventDrivenSimulator(
            candles=candles,
            cost_model=self.cost_model,
            is_news_window=self.is_news_window,
            bar_seconds=self.bar_seconds,
        )
        filtered_result = filtered_sim.run(
            wrapper,
            list(feature_packs),
            start=start,
            end=end,
        )

        filter_alpha = filtered_result.total_return - baseline_result.total_return
        llm_cost_usd = cost_tracker.estimated_usd()
        net_alpha_usd = filter_alpha * self.initial_equity_usd - llm_cost_usd

        return ABResult(
            baseline=baseline_result,
            filtered=filtered_result,
            filter_alpha=filter_alpha,
            llm_cost_usd=llm_cost_usd,
            net_alpha_usd=net_alpha_usd,
            trades_filtered_out=wrapper.filtered_out_count,
            trades_approved=wrapper.approved_count,
            cache_hit_rate=wrapper.cache_hit_rate(),
            cost_tracker=cost_tracker,
            filter_stats=wrapper.stats(),
        )

    # ---------------------------------------------------------------- helpers

    def _materialise_feature_packs(
        self,
        candles: pd.DataFrame,
        start: Optional[pd.Timestamp],
        end: Optional[pd.Timestamp],
    ) -> list:
        """Produce the per-bar feature dict list. Same list is passed to both arms.

        We deliberately produce shallow dicts here (the simulator's on_bar
        contract) and reserve the rich replayer output for the brain call
        (invoked separately by LLMFilterStrategy). Keeps the simulator hot
        loop cheap.
        """
        df = candles
        if start is not None:
            df = df.loc[df.index >= start]
        if end is not None:
            df = df.loc[df.index <= end]
        return [{} for _ in range(len(df))]

    @staticmethod
    def _clone_strategy(strategy: Strategy) -> Strategy:
        """Best-effort clone for stateful strategies passed without a factory.

        Falls back to the original object if deepcopy fails — some Strategies
        hold non-picklable objects (open connections, thread locks). Callers
        who need strict reproducibility should use ``strategy_factory``.
        """
        try:
            return copy.deepcopy(strategy)
        except Exception:
            return strategy
