"""Top-level hypothesis evaluation runner.

Wires together: walk-forward + CPCV + metrics → PASS/FAIL verdict
against the v9 plan's promotion gate:

    PASS requires ALL of:
        pbo < 0.20
        dsr > 0.95
        wfe_pass_ratio >= 0.70
        min wfe_per_step >= 0.5
        trades aggregated across OOS >= 200

The hypothesis YAML parser is owned by Track C (research/hypothesis_forge);
this runner accepts either a Path to a YAML file (loaded with PyYAML if
available) or a pre-parsed dict.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

import pandas as pd

from src.backtest.cost_model import CostModel
from src.backtest.cpcv import CombinatorialPurgedCV
from src.backtest.metrics import compute_deflated_sharpe
from src.backtest.simulator import EventDrivenSimulator
from src.backtest.strategy import Strategy
from src.backtest.walk_forward import WalkForwardRunner


# Hardcoded promotion gate, per plan/edge_generation_workflow.md
PROMOTION_GATE = {
    "pbo_max": 0.20,
    "dsr_min": 0.95,
    "wfe_pass_ratio_min": 0.70,
    "wfe_per_step_min": 0.5,
    "min_oos_trades": 200,
}


@dataclass
class HypothesisVerdict:
    hypothesis_id: str
    verdict: str  # "PASS" or "FAIL"
    fail_reasons: list = field(default_factory=list)
    in_sample: Dict[str, Any] = field(default_factory=dict)
    walk_forward: Dict[str, Any] = field(default_factory=dict)
    pbo: float = 0.0
    dsr: float = 0.0
    aggregated_trades: int = 0


class BacktestRunner:
    """Top-level entry point for hypothesis evaluation.

    Parameters
    ----------
    strategy_factory:
        Callable taking the parsed hypothesis dict and returning a Strategy.
        Track D wires this; for Phase 0 / tests a trivial factory is fine.
    cost_model:
        Optional override; defaults to plan-defined Bybit-XAUUSD params.
    bar_seconds:
        Bar duration in seconds. Default 5min.
    walk_forward_geometry:
        Tuple (is_window_bars, oos_window_bars, step_bars).
    cpcv_params:
        Dict overriding CPCV defaults (n_groups, k_test, embargo_pct,
        label_horizon_bars).
    """

    def __init__(
        self,
        strategy_factory: Callable[[Dict[str, Any]], Strategy],
        cost_model: Optional[CostModel] = None,
        bar_seconds: int = 300,
        walk_forward_geometry: tuple = (8_000, 1_000, 1_000),
        cpcv_params: Optional[Dict[str, Any]] = None,
        is_news_window: Callable[[pd.Timestamp], bool] = lambda _ts: False,
    ) -> None:
        self.strategy_factory = strategy_factory
        self.cost_model = cost_model or CostModel()
        self.bar_seconds = int(bar_seconds)
        self.walk_forward_geometry = walk_forward_geometry
        self.cpcv_params = cpcv_params or {
            "n_groups": 10,
            "k_test": 2,
            "embargo_pct": 0.01,
            "label_horizon_bars": 30,
        }
        self.is_news_window = is_news_window

    # ------------------------------------------------ public API

    def evaluate_hypothesis(
        self,
        hypothesis_yaml: Union[Path, str, Dict[str, Any]],
        candles: pd.DataFrame,
        feature_pack_provider: Callable[[pd.Timestamp], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Run walk-forward + CPCV and return the verdict dict (see contract)."""
        hypothesis = self._load_hypothesis(hypothesis_yaml)
        strategy = self.strategy_factory(hypothesis)

        simulator = EventDrivenSimulator(
            candles=candles,
            cost_model=self.cost_model,
            is_news_window=self.is_news_window,
            bar_seconds=self.bar_seconds,
        )

        # ----- in-sample run (full dataset) -----
        in_sample_features = [feature_pack_provider(ts) for ts in candles.index]
        is_result = simulator.run(strategy, in_sample_features)

        # ----- walk-forward -----
        is_bars, oos_bars, step_bars = self.walk_forward_geometry
        # If the dataset is smaller than the requested geometry, scale geometry.
        n = len(candles)
        if n < (is_bars + oos_bars):
            # Use 70/30 windowing with a single step.
            is_bars = max(1, int(0.7 * n))
            oos_bars = max(1, n - is_bars)
            step_bars = oos_bars
        wf_runner = WalkForwardRunner(
            simulator=simulator,
            is_window_bars=is_bars,
            oos_window_bars=oos_bars,
            step_bars=step_bars,
        )
        wf_result = wf_runner.run(strategy, feature_pack_provider)

        # ----- CPCV -----
        cpcv = CombinatorialPurgedCV(
            simulator=simulator,
            n_groups=self.cpcv_params.get("n_groups", 10),
            k_test=self.cpcv_params.get("k_test", 2),
            embargo_pct=self.cpcv_params.get("embargo_pct", 0.01),
            label_horizon_bars=self.cpcv_params.get("label_horizon_bars", 30),
        )
        cpcv_result = cpcv.run(strategy, feature_pack_provider)

        # ----- DSR from in-sample run -----
        returns = is_result.equity_curve.pct_change().dropna()
        n_samples = max(2, len(returns))
        from src.backtest.metrics import _safe_periods_per_year_from_bar_seconds

        ppy = _safe_periods_per_year_from_bar_seconds(self.bar_seconds)
        # Use non-annualized SR (per-period) for DSR.
        sr_per_period = float(returns.mean() / returns.std(ddof=1)) if (len(returns) > 1 and returns.std(ddof=1) != 0) else 0.0
        skew = float(returns.skew()) if len(returns) > 2 else 0.0
        # pandas .kurtosis() is excess kurtosis; DSR formula expects raw kurt.
        kurt = float(returns.kurtosis() + 3.0) if len(returns) > 3 else 3.0
        n_trials = self.cpcv_params.get("n_strategies", 1)
        dsr = compute_deflated_sharpe(
            observed_sr=sr_per_period,
            n_trials=max(1, int(n_trials)),
            n_samples=n_samples,
            skew=skew,
            kurt=kurt,
        )

        # ----- aggregate OOS trades -----
        aggregated_trades = sum(int(p.metrics.get("n_trades", 0)) for p in cpcv_result.paths)

        # ----- verdict -----
        fail_reasons = []
        if cpcv_result.pbo >= PROMOTION_GATE["pbo_max"]:
            fail_reasons.append(f"pbo {cpcv_result.pbo:.3f} >= {PROMOTION_GATE['pbo_max']}")
        if dsr <= PROMOTION_GATE["dsr_min"]:
            fail_reasons.append(f"dsr {dsr:.3f} <= {PROMOTION_GATE['dsr_min']}")
        if wf_result.wfe_pass_ratio < PROMOTION_GATE["wfe_pass_ratio_min"]:
            fail_reasons.append(
                f"wfe_pass_ratio {wf_result.wfe_pass_ratio:.3f} < {PROMOTION_GATE['wfe_pass_ratio_min']}"
            )
        if wf_result.per_step:
            min_wfe = min(s["wfe"] for s in wf_result.per_step)
            if min_wfe < PROMOTION_GATE["wfe_per_step_min"]:
                fail_reasons.append(
                    f"min wfe {min_wfe:.3f} < {PROMOTION_GATE['wfe_per_step_min']}"
                )
        if aggregated_trades < PROMOTION_GATE["min_oos_trades"]:
            fail_reasons.append(
                f"aggregated OOS trades {aggregated_trades} < {PROMOTION_GATE['min_oos_trades']}"
            )
        verdict = "PASS" if not fail_reasons else "FAIL"

        return {
            "hypothesis_id": hypothesis.get("id", "unknown"),
            "in_sample": {
                "sharpe": float(is_result.metrics.get("sharpe", 0.0)),
                "trades": int(is_result.metrics.get("n_trades", 0)),
                "win_rate": float(is_result.metrics.get("win_rate", 0.0)),
                "total_return": float(is_result.total_return),
                "max_drawdown": float(is_result.metrics.get("max_drawdown", 0.0)),
            },
            "walk_forward": wf_result.to_dict(),
            "cpcv": cpcv_result.to_dict(),
            "pbo": cpcv_result.pbo,
            "dsr": dsr,
            "aggregated_oos_trades": int(aggregated_trades),
            "verdict": verdict,
            "fail_reasons": fail_reasons,
            "gate": PROMOTION_GATE,
        }

    # ------------------------------------------------ helpers

    def _load_hypothesis(
        self, h: Union[Path, str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        if isinstance(h, dict):
            return h
        path = Path(h)
        if not path.exists():
            raise FileNotFoundError(f"hypothesis file not found: {path}")
        text = path.read_text()
        # Prefer YAML if available; fall back to JSON.
        try:
            import yaml  # type: ignore

            return yaml.safe_load(text)
        except Exception:
            return json.loads(text)
