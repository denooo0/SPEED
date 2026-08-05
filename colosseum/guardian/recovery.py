"""Checkpoints, rollback, and replay-rebuild: how the engine re-finds its path.

The central claim this module makes good on:

    THE ENGINE CAN ALWAYS RECONSTRUCT CORRECT STATE FROM THE IMMUTABLE LOG.

If that's true, no crash, drift, or corruption is terminal -- the engine walks
back to a checkpoint it can prove was good, re-derives forward deterministically,
and verifies the result by hash before it is allowed to publish again.

The repair ladder (cheap -> expensive), climbed only as far as needed:

    0 reseat_hot        flush forming bars, re-seal frames
    1 rollback_params   revert parameter policy / seat weights to last good
    2 rollback_state    restore full engine state from last verified checkpoint
    3 replay_rebuild    discard memory, re-stream the log, verify by hash
    4 rollback_roster   demote champion seat, restore prior champion
    5 halt              SAFE. Refusing to trade is always available.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..core.ids import content_hash
from ..core.types import Health

LADDER = ["reseat_hot", "rollback_params", "rollback_state",
          "replay_rebuild", "rollback_roster", "halt"]


@dataclass
class Checkpoint:
    checkpoint_id: str
    t_ns: int
    session_date: str
    state_hash: str
    verified: bool
    code_version: str
    schema_version: str
    payload: Dict[str, Any]
    metrics_snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"checkpoint_id": self.checkpoint_id, "t_ns": self.t_ns,
                "session_date": self.session_date, "state_hash": self.state_hash,
                "verified": self.verified, "code_version": self.code_version,
                "schema_version": self.schema_version, "payload": self.payload,
                "metrics_snapshot": self.metrics_snapshot}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Checkpoint":
        return Checkpoint(**d)


class CheckpointRegistry:
    """Snapshots of full engine state, content-hashed and marked verified only
    when the engine was provably HEALTHY at capture time.

    Rolling back to an unverified checkpoint is how you restore a broken state
    and call it a fix -- so `last_verified()` is the only thing recovery trusts.
    """

    def __init__(self, root: str | Path, keep: int = 200):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.keep = keep

    def _path(self, cid: str) -> Path:
        return self.root / f"{cid}.json"

    def save(self, t_ns: int, session_date: str, payload: Dict[str, Any],
             verified: bool, code_version: str, schema_version: str,
             metrics: Optional[Dict[str, Any]] = None) -> Checkpoint:
        h = content_hash(payload)
        cid = f"CKP-{t_ns}-{h[:8]}"
        cp = Checkpoint(cid, t_ns, session_date, h, verified, code_version,
                        schema_version, payload, metrics or {})
        p = self._path(cid)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cp.to_dict(), sort_keys=True, indent=2))
        tmp.replace(p)
        self._prune()
        return cp

    def _prune(self) -> None:
        files = sorted(self.root.glob("CKP-*.json"))
        # Never prune a verified checkpoint if it's the newest verified one --
        # that's the engine's lifeline.
        if len(files) <= self.keep:
            return
        verified_newest = None
        for f in reversed(files):
            try:
                if json.loads(f.read_text()).get("verified"):
                    verified_newest = f
                    break
            except Exception:
                continue
        for f in files[:len(files) - self.keep]:
            if f != verified_newest:
                f.unlink(missing_ok=True)

    def all(self) -> List[Checkpoint]:
        out = []
        for f in sorted(self.root.glob("CKP-*.json")):
            try:
                out.append(Checkpoint.from_dict(json.loads(f.read_text())))
            except Exception:
                continue
        return out

    def last(self) -> Optional[Checkpoint]:
        a = self.all()
        return a[-1] if a else None

    def last_verified(self) -> Optional[Checkpoint]:
        for cp in reversed(self.all()):
            if cp.verified:
                return cp
        return None

    def before(self, t_ns: int, verified_only: bool = True) -> Optional[Checkpoint]:
        for cp in reversed(self.all()):
            if cp.t_ns <= t_ns and (cp.verified or not verified_only):
                return cp
        return None


@dataclass
class RepairResult:
    rung: str
    success: bool
    detail: str
    state_hash_before: str = ""
    state_hash_after: str = ""
    verified: bool = False


class Recovery:
    """Climbs the repair ladder, verifying at each rung.

    Handlers are injected by the engine (this module stays free of engine
    internals so it can be tested in isolation and reused for backtests).
    """

    def __init__(self, registry: CheckpointRegistry,
                 handlers: Optional[Dict[str, Callable[[Optional[Checkpoint]], RepairResult]]] = None):
        self.registry = registry
        self.handlers: Dict[str, Callable] = handlers or {}
        self.history: List[RepairResult] = []
        self.fault_memory: Dict[str, List[str]] = {}   # immune memory

    def register(self, rung: str, fn: Callable[[Optional[Checkpoint]], RepairResult]) -> None:
        if rung not in LADDER:
            raise ValueError(f"unknown rung {rung}; must be one of {LADDER}")
        self.handlers[rung] = fn

    def repair(self, detector: str, start_rung: Optional[str] = None,
               max_rung: str = "halt") -> List[RepairResult]:
        """Climb until something verifies, or until max_rung.

        Immune memory: if this fault signature has been seen before, jump
        straight to the rung that fixed it last time. Repeated faults get faster,
        pre-diagnosed responses instead of re-deriving the ladder each time.
        """
        results: List[RepairResult] = []
        remembered = self.fault_memory.get(detector)
        begin = LADDER.index(remembered[-1]) if remembered else \
            (LADDER.index(start_rung) if start_rung else 0)
        stop = LADDER.index(max_rung)

        for rung in LADDER[begin:stop + 1]:
            fn = self.handlers.get(rung)
            if fn is None:
                results.append(RepairResult(rung, False, "no handler registered"))
                continue
            cp = self.registry.last_verified()
            try:
                r = fn(cp)
            except Exception as e:                      # a failing repair must
                r = RepairResult(rung, False, f"handler raised: {e!r}")  # never
            results.append(r)                            # crash the engine
            self.history.append(r)
            if r.success and r.verified:
                self.fault_memory.setdefault(detector, []).append(rung)
                self.fault_memory[detector] = self.fault_memory[detector][-5:]
                break
        return results

    def suggested_state(self, results: List[RepairResult]) -> Health:
        if not results:
            return Health.SAFE
        last = results[-1]
        if last.success and last.verified:
            return Health.DEGRADED        # probation, never straight to HEALTHY
        if last.rung == "halt":
            return Health.SAFE
        return Health.RECOVERING


def verify_replay(live_hash: str, replay_hash: str) -> bool:
    """The verification gate. The engine does NOT return to publishing until the
    state it rebuilt matches a deterministic replay of the same inputs. This is
    what turns 'probably fine' into 'proven'."""
    return bool(live_hash) and live_hash == replay_hash
