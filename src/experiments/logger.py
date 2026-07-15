"""Experiment logger — every backtest run is a logged scientific experiment.

Storage layout
--------------
::

    experiments/log/<experiment_id>.json          one file per experiment
    experiments/log_index.parquet                 (optional) fast-query index

Each JSON file contains:

* ``experiment_id`` — uuid4 hex, mints when :meth:`ExperimentLogger.start` runs.
* ``hypothesis_id`` — the registry ID of the hypothesis under test.
* ``started_at`` / ``finished_at`` — ISO-8601 UTC.
* ``config_snapshot`` — arbitrary dict caller supplies (feature pack, cost
  model, etc.). Copied verbatim.
* ``events`` — list of ``{ts, key, value}`` dicts appended via
  :meth:`ExperimentLogger.log`.
* ``result`` — populated on :meth:`ExperimentLogger.finish`; typically the
  :class:`~src.backtest.runner.BacktestRunner` verdict dict.

The parquet index is opportunistically maintained (best-effort). If it goes
missing or gets stale, :meth:`ExperimentLogger.query` still works from the
JSON files directly.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


DEFAULT_ROOT = Path("experiments/log/")
INDEX_FILENAME = "log_index.parquet"


# ---------------------------------------------------------------- serialisation


def _json_default(obj: Any) -> Any:
    """Best-effort fallback for arbitrary payloads.

    Timestamps -> ISO-8601. numpy scalars -> Python scalars.
    Sets / tuples -> lists. Everything else -> its repr.
    """
    if isinstance(obj, (pd.Timestamp, datetime)):
        return _to_iso(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=lambda x: repr(x))
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, Path):
        return str(obj)
    for attr in ("tolist", "item"):
        if hasattr(obj, attr):
            try:
                return getattr(obj, attr)()
            except Exception:
                pass
    try:
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
    except TypeError:
        pass
    return repr(obj)


def _to_iso(ts: pd.Timestamp | datetime | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, pd.Timestamp):
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.isoformat()
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()
    return str(ts)


def _from_iso(text: str | None) -> pd.Timestamp | None:
    if text in (None, ""):
        return None
    ts = pd.Timestamp(text)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts


# ---------------------------------------------------------------- Experiment


@dataclass
class Experiment:
    experiment_id: str
    hypothesis_id: str
    started_at: pd.Timestamp
    finished_at: pd.Timestamp | None
    config_snapshot: dict
    result: dict | None
    events: list[dict] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "started_at": _to_iso(self.started_at),
            "finished_at": _to_iso(self.finished_at),
            "config_snapshot": self.config_snapshot,
            "result": self.result,
            "events": self.events,
        }

    @classmethod
    def from_json_dict(cls, raw: dict) -> "Experiment":
        return cls(
            experiment_id=str(raw["experiment_id"]),
            hypothesis_id=str(raw.get("hypothesis_id", "")),
            started_at=_from_iso(raw.get("started_at")) or pd.Timestamp.now(tz="UTC"),
            finished_at=_from_iso(raw.get("finished_at")),
            config_snapshot=dict(raw.get("config_snapshot") or {}),
            result=raw.get("result"),
            events=list(raw.get("events") or []),
        )


# ---------------------------------------------------------------- Logger


class ExperimentLogger:
    """One JSON per experiment on disk, with an optional parquet index."""

    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root.parent / INDEX_FILENAME

    # -------------------------- disk helpers

    def _path(self, experiment_id: str) -> Path:
        return self.root / f"{experiment_id}.json"

    def _read(self, experiment_id: str) -> dict:
        path = self._path(experiment_id)
        if not path.exists():
            raise FileNotFoundError(f"experiment {experiment_id!r} not found at {path}")
        return json.loads(path.read_text())

    def _write(self, experiment_id: str, payload: dict) -> None:
        path = self._path(experiment_id)
        path.write_text(json.dumps(payload, default=_json_default, indent=2))

    def _refresh_index(self) -> None:
        """Rebuild ``experiments/log_index.parquet`` from all JSON files.

        Best-effort — failures are swallowed. The parquet index is a fast
        query cache, not a source of truth.
        """
        try:
            rows = []
            for exp in self._iter_all():
                rows.append(
                    {
                        "experiment_id": exp.experiment_id,
                        "hypothesis_id": exp.hypothesis_id,
                        "started_at": exp.started_at,
                        "finished_at": exp.finished_at,
                        "verdict": (exp.result or {}).get("verdict"),
                        "n_events": len(exp.events),
                    }
                )
            df = (
                pd.DataFrame(rows)
                if rows
                else pd.DataFrame(
                    columns=[
                        "experiment_id",
                        "hypothesis_id",
                        "started_at",
                        "finished_at",
                        "verdict",
                        "n_events",
                    ]
                )
            )
            df.to_parquet(self.index_path, index=False)
        except Exception:
            pass

    def _iter_all(self) -> Iterable[Experiment]:
        for path in sorted(self.root.glob("*.json")):
            try:
                yield Experiment.from_json_dict(json.loads(path.read_text()))
            except Exception:
                continue

    # -------------------------- public API

    def start(self, hypothesis_id: str, config: dict) -> str:
        """Mint an experiment_id, persist the stub, return the id."""
        experiment_id = "exp_" + uuid.uuid4().hex[:16]
        payload = {
            "experiment_id": experiment_id,
            "hypothesis_id": hypothesis_id,
            "started_at": _to_iso(pd.Timestamp.now(tz="UTC")),
            "finished_at": None,
            "config_snapshot": dict(config or {}),
            "result": None,
            "events": [],
        }
        self._write(experiment_id, payload)
        self._refresh_index()
        return experiment_id

    def log(self, experiment_id: str, key: str, value: Any) -> None:
        """Append an event ``{ts, key, value}`` to the experiment's log."""
        payload = self._read(experiment_id)
        event = {
            "ts": _to_iso(pd.Timestamp.now(tz="UTC")),
            "key": str(key),
            "value": value,
        }
        payload.setdefault("events", []).append(event)
        self._write(experiment_id, payload)

    def finish(self, experiment_id: str, result: dict) -> None:
        """Attach ``finished_at`` + ``result`` and persist."""
        payload = self._read(experiment_id)
        payload["finished_at"] = _to_iso(pd.Timestamp.now(tz="UTC"))
        payload["result"] = dict(result or {})
        self._write(experiment_id, payload)
        self._refresh_index()

    def load(self, experiment_id: str) -> Experiment:
        return Experiment.from_json_dict(self._read(experiment_id))

    def query(self, filter: dict | None = None) -> list[Experiment]:
        """Return every experiment matching a shallow filter.

        Filter keys are matched against Experiment attributes AND the
        ``result`` dict — so ``{"verdict": "PASS"}`` is equivalent to
        ``result["verdict"] == "PASS"``.
        """
        f = dict(filter or {})
        results: list[Experiment] = []
        for exp in self._iter_all():
            if self._matches(exp, f):
                results.append(exp)
        return results

    @staticmethod
    def _matches(exp: Experiment, filter: dict) -> bool:
        for key, expected in filter.items():
            actual = None
            if hasattr(exp, key):
                actual = getattr(exp, key)
            elif exp.result and key in exp.result:
                actual = exp.result[key]
            elif key in exp.config_snapshot:
                actual = exp.config_snapshot[key]
            else:
                return False
            if actual != expected:
                return False
        return True
