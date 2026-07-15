"""Tests for RegimeArchiveLoader + build_synthetic_regime_archives."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.meta.regime_archives import (
    REGIME_NAMES,
    RegimeArchive,
    RegimeArchiveLoader,
    RegimeArchiveMissing,
    build_synthetic_regime_archives,
)


# ------------------------------------------------------------- builder


def test_build_creates_three_parquets(tmp_path: Path):
    paths = build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    assert set(paths.keys()) == {"trending_bull", "trending_bear", "sideways"}
    for name in REGIME_NAMES:
        p = tmp_path / f"{name}.parquet"
        assert p.exists()
        df = pd.read_parquet(p)
        # 500 bars each with the OHLCV schema the simulator expects.
        assert len(df) == 500
        for col in ("open", "high", "low", "close", "volume"):
            assert col in df.columns
        assert df.index.is_monotonic_increasing


def test_build_is_deterministic(tmp_path: Path):
    p1 = build_synthetic_regime_archives(root=tmp_path / "a", overwrite=True)
    p2 = build_synthetic_regime_archives(root=tmp_path / "b", overwrite=True)
    for name in REGIME_NAMES:
        a = pd.read_parquet(p1[name])
        b = pd.read_parquet(p2[name])
        pd.testing.assert_frame_equal(a, b)


def test_build_idempotent_without_overwrite(tmp_path: Path):
    build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    mtimes_before = {
        n: (tmp_path / f"{n}.parquet").stat().st_mtime for n in REGIME_NAMES
    }
    # Second call without overwrite must not touch existing files.
    build_synthetic_regime_archives(root=tmp_path, overwrite=False)
    mtimes_after = {
        n: (tmp_path / f"{n}.parquet").stat().st_mtime for n in REGIME_NAMES
    }
    assert mtimes_before == mtimes_after


def test_build_trending_bull_ends_higher_than_start(tmp_path: Path):
    build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    df = pd.read_parquet(tmp_path / "trending_bull.parquet")
    assert df["close"].iloc[-1] > df["close"].iloc[0]


def test_build_trending_bear_ends_lower_than_start(tmp_path: Path):
    build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    df = pd.read_parquet(tmp_path / "trending_bear.parquet")
    assert df["close"].iloc[-1] < df["close"].iloc[0]


def test_build_sideways_stays_near_starting_price(tmp_path: Path):
    build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    df = pd.read_parquet(tmp_path / "sideways.parquet")
    # Mean-reverting: last close within ±10% of first close.
    change_pct = abs(df["close"].iloc[-1] / df["close"].iloc[0] - 1.0)
    assert change_pct < 0.10, f"sideways drift too large: {change_pct:.3f}"


# ------------------------------------------------------------- loader


def test_load_returns_regime_archive(tmp_path: Path):
    build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    loader = RegimeArchiveLoader(root=tmp_path)
    arc = loader.load("trending_bull")
    assert isinstance(arc, RegimeArchive)
    assert arc.name == "trending_bull"
    assert isinstance(arc.candles, pd.DataFrame)
    assert arc.description  # non-empty


def test_load_all_returns_all_three(tmp_path: Path):
    build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    loader = RegimeArchiveLoader(root=tmp_path)
    arcs = loader.load_all()
    assert [a.name for a in arcs] == list(REGIME_NAMES)


def test_load_missing_archive_raises(tmp_path: Path):
    loader = RegimeArchiveLoader(root=tmp_path)
    with pytest.raises(RegimeArchiveMissing):
        loader.load("trending_bull")


def test_load_unknown_regime_raises(tmp_path: Path):
    build_synthetic_regime_archives(root=tmp_path, overwrite=True)
    loader = RegimeArchiveLoader(root=tmp_path)
    with pytest.raises(RegimeArchiveMissing):
        loader.load("moon_landing")


def test_default_root_points_to_repo_fixtures():
    # Sanity check: the default root exists in the repo after Track E lands.
    loader = RegimeArchiveLoader()
    assert loader.root == Path("data/regimes/")
