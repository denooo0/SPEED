"""Regime archives for the meta-loop backtest oracle.

Safety rail 4 (see plan/meta_learning_loop.md §IV.4): every proposed
change must be replayed against three regime archives — a trending
bull, trending bear, and sideways / choppy tape. A change that
degrades meaningfully in ANY regime is blocked; there are no
per-regime opt-outs.

This module owns the on-disk fixtures. Each archive is a 500-bar
parquet under `data/regimes/`:

    trending_bull.parquet   — positive drift + noise (up-trend)
    trending_bear.parquet   — negative drift + noise (down-trend)
    sideways.parquet        — Ornstein-Uhlenbeck mean reversion (chop)

Bar columns: open, high, low, close, volume — the same schema the
BacktestRunner / EventDrivenSimulator expects. The archives are
synthetic (not sourced from the live tape) so the meta-loop's holdout
window is untouched and the fixtures are reproducible from a seed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd


REGIME_NAMES = ("trending_bull", "trending_bear", "sideways")

# Fixed descriptions per regime (baked into the loader for convenience —
# the parquets themselves don't need metadata columns).
_DESCRIPTIONS: Dict[str, str] = {
    "trending_bull": "500-bar synthetic uptrend: positive drift + noise",
    "trending_bear": "500-bar synthetic downtrend: negative drift + noise",
    "sideways": "500-bar synthetic chop: Ornstein-Uhlenbeck mean-reverting price",
}


@dataclass
class RegimeArchive:
    name: str
    candles: pd.DataFrame
    description: str


class RegimeArchiveMissing(FileNotFoundError):
    """Raised when a requested regime archive is not on disk."""


class RegimeArchiveLoader:
    """Loads on-disk regime parquets.

    Callers should invoke `build_synthetic_regime_archives()` once (test
    setup, first run) to materialize the parquets; subsequent runs read
    from disk deterministically.
    """

    def __init__(self, root: Path = Path("data/regimes/")) -> None:
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.parquet"

    def load(self, name: str) -> RegimeArchive:
        p = self._path(name)
        if not p.exists():
            raise RegimeArchiveMissing(
                f"regime archive not found: {p} "
                f"(run build_synthetic_regime_archives to materialize)"
            )
        candles = pd.read_parquet(p)
        return RegimeArchive(
            name=name,
            candles=candles,
            description=_DESCRIPTIONS.get(name, ""),
        )

    def load_all(self) -> List[RegimeArchive]:
        return [self.load(n) for n in REGIME_NAMES]


# ----------------------------------------------------------- synthetic builder


def _bar_frame(index: pd.DatetimeIndex, closes: np.ndarray, volume: float = 50.0) -> pd.DataFrame:
    """Build an OHLCV frame from a close-series (opens lag closes by 1)."""
    n = len(closes)
    opens = np.empty(n, dtype=float)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.001,
            "low": np.minimum(opens, closes) * 0.999,
            "close": closes,
            "volume": np.full(n, float(volume)),
        },
        index=index,
    )


def _trending_series(
    n: int,
    drift_per_bar: float,
    vol_per_bar: float,
    seed: int,
    start_price: float = 2000.0,
) -> np.ndarray:
    """Geometric random walk with a directional drift term."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(drift_per_bar, vol_per_bar, n)
    closes = start_price * np.cumprod(1.0 + returns)
    return closes


def _sideways_series(
    n: int,
    kappa: float,
    sigma: float,
    seed: int,
    target_price: float = 2000.0,
) -> np.ndarray:
    """Ornstein-Uhlenbeck mean-reverting price path (absolute, not returns)."""
    rng = np.random.default_rng(seed)
    prices = np.zeros(n, dtype=float)
    prices[0] = target_price
    for i in range(1, n):
        prices[i] = prices[i - 1] + kappa * (target_price - prices[i - 1]) + rng.normal(0.0, sigma)
    return prices


def build_synthetic_regime_archives(
    root: Path = Path("data/regimes/"),
    n_bars: int = 500,
    freq: str = "5min",
    start: str = "2024-01-01",
    overwrite: bool = False,
) -> Dict[str, Path]:
    """Generate the 3 regime parquets under `root`.

    Deterministic via fixed seeds. Idempotent: if the target parquet
    exists and `overwrite=False`, it is left in place.

    Returns
    -------
    dict[str, Path]
        Map of regime name -> parquet path.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    index = pd.date_range(start=start, periods=n_bars, freq=freq)

    written: Dict[str, Path] = {}

    def _emit(name: str, closes: np.ndarray) -> None:
        path = root / f"{name}.parquet"
        if path.exists() and not overwrite:
            written[name] = path
            return
        frame = _bar_frame(index, closes)
        frame.to_parquet(path)
        written[name] = path

    # Trending bull: +0.0006 drift, 0.001 vol → ~ +35% over 500 bars.
    bull = _trending_series(n_bars, drift_per_bar=0.0006, vol_per_bar=0.0010, seed=42)
    _emit("trending_bull", bull)

    # Trending bear: -0.0006 drift, symmetric noise.
    bear = _trending_series(n_bars, drift_per_bar=-0.0006, vol_per_bar=0.0010, seed=43)
    _emit("trending_bear", bear)

    # Sideways: mean-revert around 2000 with tight vol.
    side = _sideways_series(n_bars, kappa=0.05, sigma=1.0, seed=44)
    _emit("sideways", side)

    return written
