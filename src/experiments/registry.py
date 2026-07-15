"""Hypothesis registry — every hypothesis ever tested lives here forever.

Motivation
----------
The Deflated Sharpe Ratio computed in :mod:`src.backtest.metrics` needs an
accurate ``n_trials`` — the number of *independent variants explored*. If we
undercount, DSR overstates significance and we ship an overfit strategy.

This registry is the single source of truth for that count. Every backtest
launched anywhere in the codebase must first ``register()`` its hypothesis
(idempotent — same hypothesis returns the same id) and then ``record_trial()``
the resulting Sharpe / PBO.

Persistence
-----------
On disk as a single parquet file at ``experiments/hypotheses.parquet``.
Loaded eagerly on construction; every mutation rewrites the file. This is
fine at the volumes we care about (thousands, not millions, of hypotheses
across the project's life).
"""
from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


DEFAULT_PATH = Path("experiments/hypotheses.parquet")


@dataclass
class HypothesisEntry:
    hypothesis_id: str
    hypothesis_text: str  # canonical JSON string
    hypothesis_hash: str  # sha256 of canonical form
    first_seen: pd.Timestamp
    trial_count: int
    best_sharpe: float | None
    best_pbo: float | None


# ---------------------------------------------------------------- helpers


def _canonicalize(hypothesis: dict | str) -> str:
    """Return a canonical JSON string for hashing / persistence.

    ``dict`` -> ``json.dumps(sort_keys=True)``.
    ``str``  -> either passed-through JSON (re-serialized sorted) or wrapped as
                ``{"text": ...}`` when not JSON.
    """
    if isinstance(hypothesis, dict):
        return json.dumps(hypothesis, sort_keys=True, default=str)
    if isinstance(hypothesis, str):
        try:
            parsed = json.loads(hypothesis)
            if isinstance(parsed, dict):
                return json.dumps(parsed, sort_keys=True, default=str)
        except (ValueError, TypeError):
            pass
        return json.dumps({"text": hypothesis}, sort_keys=True)
    raise TypeError(f"hypothesis must be dict or str, got {type(hypothesis)!r}")


def _hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _short_id(canonical_hash: str) -> str:
    return f"hyp_{canonical_hash[:12]}"


def _to_dict(canonical: str) -> dict:
    try:
        parsed = json.loads(canonical)
        return parsed if isinstance(parsed, dict) else {"text": str(parsed)}
    except (ValueError, TypeError):
        return {"text": canonical}


def _similarity(a: dict | str, b: dict | str) -> float:
    """Similarity in [0, 1].

    * Two strings — :class:`difflib.SequenceMatcher` ratio.
    * Two dicts — union of keys; per key: equal values contribute 1.0,
      differing strings contribute their SequenceMatcher ratio, differing
      non-strings contribute 0. Missing key contributes 0.
    * Mixed — first canonicalize both to dicts and recurse.
    """
    if isinstance(a, str) and isinstance(b, str):
        return difflib.SequenceMatcher(None, a, b).ratio()

    a_dict = a if isinstance(a, dict) else _to_dict(_canonicalize(a))
    b_dict = b if isinstance(b, dict) else _to_dict(_canonicalize(b))

    keys = set(a_dict.keys()) | set(b_dict.keys())
    if not keys:
        return 1.0

    score = 0.0
    for k in keys:
        av = a_dict.get(k)
        bv = b_dict.get(k)
        if av is None or bv is None:
            continue  # counts as 0
        if av == bv:
            score += 1.0
            continue
        if isinstance(av, str) and isinstance(bv, str):
            score += difflib.SequenceMatcher(None, av, bv).ratio()
        elif isinstance(av, dict) and isinstance(bv, dict):
            score += _similarity(av, bv)
        else:
            # Different types or non-equal non-string values — 0.
            pass
    return score / len(keys)


# ---------------------------------------------------------------- Registry


class HypothesisRegistry:
    """Tracks every hypothesis ever tested.

    Used to feed the accurate ``n_trials`` count into DSR calculations.
    Persisted as parquet; safe to reconstruct from disk across processes.
    """

    _COLUMNS = (
        "hypothesis_id",
        "hypothesis_text",
        "hypothesis_hash",
        "first_seen",
        "trial_count",
        "best_sharpe",
        "best_pbo",
    )

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, HypothesisEntry] = {}
        self._load()

    # -------------------------- persistence

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            df = pd.read_parquet(self.path)
        except Exception:
            return
        for _, row in df.iterrows():
            best_sharpe = row.get("best_sharpe")
            best_pbo = row.get("best_pbo")
            entry = HypothesisEntry(
                hypothesis_id=str(row["hypothesis_id"]),
                hypothesis_text=str(row["hypothesis_text"]),
                hypothesis_hash=str(row["hypothesis_hash"]),
                first_seen=pd.Timestamp(row["first_seen"]),
                trial_count=int(row["trial_count"]),
                best_sharpe=None if pd.isna(best_sharpe) else float(best_sharpe),
                best_pbo=None if pd.isna(best_pbo) else float(best_pbo),
            )
            self._entries[entry.hypothesis_id] = entry

    def _flush(self) -> None:
        if not self._entries:
            # Write an empty typed frame so the file exists with the schema.
            df = pd.DataFrame(columns=list(self._COLUMNS))
        else:
            rows = [
                {
                    "hypothesis_id": e.hypothesis_id,
                    "hypothesis_text": e.hypothesis_text,
                    "hypothesis_hash": e.hypothesis_hash,
                    "first_seen": e.first_seen,
                    "trial_count": e.trial_count,
                    "best_sharpe": e.best_sharpe,
                    "best_pbo": e.best_pbo,
                }
                for e in self._entries.values()
            ]
            df = pd.DataFrame(rows, columns=list(self._COLUMNS))
        df.to_parquet(self.path, index=False)

    # -------------------------- public API

    def register(self, hypothesis: dict | str) -> str:
        """Register a hypothesis, returning its stable ID.

        Idempotent — the same canonical hypothesis returns the same ID and
        does not create a duplicate row.
        """
        canonical = _canonicalize(hypothesis)
        h = _hash(canonical)
        hyp_id = _short_id(h)
        if hyp_id in self._entries:
            return hyp_id
        entry = HypothesisEntry(
            hypothesis_id=hyp_id,
            hypothesis_text=canonical,
            hypothesis_hash=h,
            first_seen=pd.Timestamp.now(tz="UTC"),
            trial_count=0,
            best_sharpe=None,
            best_pbo=None,
        )
        self._entries[hyp_id] = entry
        self._flush()
        return hyp_id

    def record_trial(self, hypothesis_id: str, sharpe: float, pbo: float) -> None:
        """Increment trial_count and roll best_sharpe / best_pbo.

        ``best_sharpe`` = max seen. ``best_pbo`` = min seen (lower is better —
        PBO is the *probability* of overfitting).
        """
        if hypothesis_id not in self._entries:
            raise KeyError(f"unknown hypothesis_id: {hypothesis_id}")
        entry = self._entries[hypothesis_id]
        entry.trial_count += 1
        sharpe = float(sharpe)
        pbo = float(pbo)
        if entry.best_sharpe is None or sharpe > entry.best_sharpe:
            entry.best_sharpe = sharpe
        if entry.best_pbo is None or pbo < entry.best_pbo:
            entry.best_pbo = pbo
        self._flush()

    def total_trials(self) -> int:
        """Sum of trial counts across every registered hypothesis.

        This is the ``n_trials`` figure that feeds DSR.
        """
        return int(sum(e.trial_count for e in self._entries.values()))

    def find_similar(
        self, hypothesis: dict | str, threshold: float = 0.9
    ) -> list[str]:
        """Return IDs of hypotheses with similarity >= ``threshold``.

        Threshold semantics (per spec):
            * 1.0 -> exact match
            * 0.0 -> matches anything in the registry

        Uses fuzzy string match on text fields and per-field similarity on
        structured dicts.
        """
        if not (0.0 <= threshold <= 1.0):
            raise ValueError("threshold must be in [0, 1]")
        canonical = _canonicalize(hypothesis)
        target_dict = _to_dict(canonical)
        hits: list[tuple[float, str]] = []
        for entry in self._entries.values():
            other_dict = _to_dict(entry.hypothesis_text)
            score = _similarity(target_dict, other_dict)
            if score >= threshold:
                hits.append((score, entry.hypothesis_id))
        hits.sort(key=lambda t: (-t[0], t[1]))
        return [hid for _, hid in hits]

    def get(self, hypothesis_id: str) -> HypothesisEntry | None:
        return self._entries.get(hypothesis_id)

    def all_entries(self) -> Iterable[HypothesisEntry]:
        return tuple(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)
