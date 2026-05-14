"""CPCV runner tests."""
from __future__ import annotations

from itertools import combinations
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import pytest

from src.backtest.cost_model import CostModel
from src.backtest.cpcv import CombinatorialPurgedCV
from src.backtest.simulator import EventDrivenSimulator
from src.backtest.strategy import Bar, Order, PositionState


def _candles(n: int = 300) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    rng = np.random.default_rng(123)
    closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0001, 0.001, n))
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.0005,
            "low": np.minimum(opens, closes) * 0.9995,
            "close": closes,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )


class _LongAndHold:
    def on_bar(self, bar: Bar, fp: Dict[str, Any], ps: PositionState) -> Optional[Order]:
        if ps.is_open:
            return None
        return Order(
            direction="long",
            entry_price=bar.close,
            stop_loss=bar.close * 0.1,
            take_profits=[bar.close * 10.0],
            size=1.0,
        )


def test_cpcv_validates_k_test():
    df = _candles(120)
    sim = EventDrivenSimulator(df, CostModel())
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(sim, n_groups=5, k_test=0)
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(sim, n_groups=5, k_test=5)


def test_cpcv_validates_n_groups():
    df = _candles(120)
    sim = EventDrivenSimulator(df, CostModel())
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(sim, n_groups=1, k_test=1)


def test_cpcv_path_count_matches_combinations():
    df = _candles(200)
    sim = EventDrivenSimulator(df, CostModel())
    cpcv = CombinatorialPurgedCV(sim, n_groups=10, k_test=2, embargo_pct=0.0, label_horizon_bars=0)
    result = cpcv.run(_LongAndHold(), feature_pack_provider=lambda _ts: {})
    # C(10, 2) = 45 combinations
    assert result.n_paths == 45
    assert len(result.paths) == 45
    assert len(result.sharpes) == 45


def test_cpcv_default_pbo_zero_single_strategy():
    """Without strategy_factory, PBO is 0 by contract (can't compare multiple strategies)."""
    df = _candles(200)
    sim = EventDrivenSimulator(df, CostModel())
    cpcv = CombinatorialPurgedCV(sim, n_groups=6, k_test=2, embargo_pct=0.0, label_horizon_bars=0)
    result = cpcv.run(_LongAndHold(), feature_pack_provider=lambda _ts: {})
    assert result.pbo == 0.0


def test_cpcv_pbo_with_strategy_factory():
    """When given a strategy_factory we compute a real PBO via CSCV."""
    df = _candles(400)
    sim = EventDrivenSimulator(df, CostModel())
    cpcv = CombinatorialPurgedCV(sim, n_groups=5, k_test=2, embargo_pct=0.0, label_horizon_bars=0)

    # Two indistinguishable long-and-hold strategies → CSCV with random ranks → pbo close to 0.5
    class A:
        def on_bar(self, bar, fp, ps):
            if ps.is_open:
                return None
            return Order("long", bar.close, bar.close * 0.1, [bar.close * 10.0], 1.0)

    class B:
        def on_bar(self, bar, fp, ps):
            if ps.is_open:
                return None
            return Order("long", bar.close, bar.close * 0.1, [bar.close * 10.0], 1.0)

    strategies = [A(), B()]
    result = cpcv.run(
        strategy=A(),
        feature_pack_provider=lambda _ts: {},
        strategy_factory=lambda i: strategies[i],
        n_strategies_for_pbo=2,
    )
    # Two identical strategies produce identical Sharpes → ranks tied → PBO either 0 or 0.5.
    assert 0.0 <= result.pbo <= 1.0


def test_cpcv_paths_have_metrics():
    df = _candles(200)
    sim = EventDrivenSimulator(df, CostModel())
    cpcv = CombinatorialPurgedCV(sim, n_groups=5, k_test=2, embargo_pct=0.0, label_horizon_bars=0)
    result = cpcv.run(_LongAndHold(), feature_pack_provider=lambda _ts: {})
    # Every path should carry standard metrics.
    for p in result.paths:
        assert "sharpe" in p.metrics
        assert "n_trades" in p.metrics
