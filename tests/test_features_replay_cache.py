"""Tests for FeatureCache.

Covers:
* get_or_compute returns identical objects on cache hit (no recomputation).
* invalidate removes the cached entry.
* Stable key for identical input args; different key for different args.
* Path-aware keying: cache invalidates when an underlying file mtime changes.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.features.cache import FeatureCache


def _make_payload(tag: str):
    """A non-trivial picklable payload (mimics a list of FeaturePacks)."""
    return [{"tag": tag, "ix": i} for i in range(5)]


def test_cache_hit_returns_identical_payload(tmp_path: Path):
    cache = FeatureCache(root=tmp_path)
    key = cache.key("xauusd", start="2026-01-01", end="2026-02-01")

    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return _make_payload("first")

    first = cache.get_or_compute(key, compute)
    second = cache.get_or_compute(key, compute)

    assert first == second
    assert calls["n"] == 1  # compute_fn must NOT be re-invoked on hit


def test_cache_key_stable_for_same_args(tmp_path: Path):
    cache = FeatureCache(root=tmp_path)
    k1 = cache.key("xauusd", tf="m5", start="2026-01-01")
    k2 = cache.key("xauusd", tf="m5", start="2026-01-01")
    assert k1 == k2


def test_cache_key_differs_for_different_args(tmp_path: Path):
    cache = FeatureCache(root=tmp_path)
    k1 = cache.key("xauusd", tf="m5", start="2026-01-01")
    k2 = cache.key("xauusd", tf="m5", start="2026-01-02")
    assert k1 != k2


def test_cache_invalidate_specific_key(tmp_path: Path):
    cache = FeatureCache(root=tmp_path)
    key = cache.key("run-1")
    cache.get_or_compute(key, lambda: _make_payload("v1"))
    assert cache.has(key)
    cache.invalidate(key)
    assert not cache.has(key)


def test_cache_invalidate_all(tmp_path: Path):
    cache = FeatureCache(root=tmp_path)
    for tag in ("a", "b", "c"):
        cache.get_or_compute(cache.key(tag), lambda t=tag: _make_payload(t))
    cache.invalidate(None)
    assert not any(tmp_path.glob("*.pkl.gz"))


def test_cache_key_changes_when_file_mtime_changes(tmp_path: Path):
    """Path-aware keying — a modified input file should invalidate the cache."""
    f = tmp_path / "candles.parquet"
    f.write_bytes(b"v1")
    cache = FeatureCache(root=tmp_path)
    k1 = cache.key("xauusd", f)

    # Bump mtime forward by 1s; size/content also changes.
    time.sleep(0.01)
    f.write_bytes(b"version-2-larger")
    # Force a different mtime even on coarse-resolution filesystems.
    new_mtime = f.stat().st_mtime + 2
    import os
    os.utime(f, (new_mtime, new_mtime))

    k2 = cache.key("xauusd", f)
    assert k1 != k2


def test_cache_handles_missing_path(tmp_path: Path):
    """Missing file paths should still produce a stable, non-crashing key."""
    cache = FeatureCache(root=tmp_path)
    missing = tmp_path / "nope.parquet"
    k1 = cache.key("xauusd", missing)
    k2 = cache.key("xauusd", missing)
    assert k1 == k2
