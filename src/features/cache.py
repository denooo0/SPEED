"""Pickled+gzipped cache of computed FeaturePacks.

Keyed by sha256 over the inputs the caller chooses to identify a run. Stored
under `data/feature_cache/<key>.pkl.gz`. Designed for big backtests where the
4-lens extraction over millions of bars is the slowest step.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import pickle
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional


class FeatureCache:
    def __init__(self, root: Path | str = Path("data/feature_cache/")) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- key building ----------------------------------------------------
    def key(self, *args: Any, **kwargs: Any) -> str:
        """Stable sha256 hex of the (args, kwargs) tuple."""
        payload = {
            "args": [self._normalize(a) for a in args],
            "kwargs": {k: self._normalize(v) for k, v in sorted(kwargs.items())},
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    @staticmethod
    def _normalize(value: Any) -> Any:
        # Path → mtime + size, so cache invalidates when the underlying file
        # changes without callers having to think about it.
        if isinstance(value, Path):
            if value.exists():
                stat = value.stat()
                return {"path": str(value), "mtime": stat.st_mtime, "size": stat.st_size}
            return {"path": str(value), "missing": True}
        return value

    # -- I/O -------------------------------------------------------------
    def _path_for(self, key: str) -> Path:
        return self.root / f"{key}.pkl.gz"

    def has(self, key: str) -> bool:
        return self._path_for(key).exists()

    def load(self, key: str) -> Optional[List[Any]]:
        p = self._path_for(key)
        if not p.exists():
            return None
        with gzip.open(p, "rb") as f:
            return pickle.load(f)

    def store(self, key: str, packs: Iterable[Any]) -> None:
        p = self._path_for(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wb") as f:
            pickle.dump(list(packs), f, protocol=pickle.HIGHEST_PROTOCOL)

    # -- high-level API --------------------------------------------------
    def get_or_compute(
        self,
        key: str,
        compute_fn: Callable[[], Iterable[Any]],
    ) -> List[Any]:
        existing = self.load(key)
        if existing is not None:
            return existing
        packs = list(compute_fn())
        self.store(key, packs)
        return packs

    def invalidate(self, key: Optional[str] = None) -> None:
        if key is None:
            for p in self.root.glob("*.pkl.gz"):
                p.unlink()
            return
        p = self._path_for(key)
        if p.exists():
            p.unlink()
