"""Replay: cold-start bootstrap and recovery verification.

Two jobs, one mechanism.

  1. COLD START. Day one has no data, no rankings, no lessons. Bootstrapping by
     replaying archived ticks through the IDENTICAL pipeline produces a real
     prior ledger before a single live signal is published. Because the pipeline
     is point-in-time sealed and no component reads a clock, these results are
     legitimate training data rather than a backtest fantasy.

  2. RECOVERY VERIFICATION. Guardian rung 3 re-derives state from the tick
     archive and compares hashes. That comparison is only meaningful if replay
     and live share one code path -- which is why replay is not a separate
     "backtester" but the same Engine in REPLAY mode.

The one thing replay must never do is publish. A replayed signal reaching
Telegram would be indistinguishable from a live one to the reader, so REPLAY
health state hard-gates delivery at zero conviction multiplier.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .core.clock import session_date
from .core.types import Health, Tick
from .engine import Engine, EngineConfig
from .feeds.base import Feed, JsonlFeed


@dataclass
class ReplayReport:
    ticks_in: int = 0
    ticks_accepted: int = 0
    frames: int = 0
    signals: int = 0
    resolved: int = 0
    sessions: List[str] = field(default_factory=list)
    final_state_hash: str = ""
    duration_s: float = 0.0
    quarantined: int = 0
    ticks_per_s: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {"ticks_in": self.ticks_in, "ticks_accepted": self.ticks_accepted,
                "frames": self.frames, "signals": self.signals,
                "resolved": self.resolved, "sessions": self.sessions,
                "final_state_hash": self.final_state_hash,
                "duration_s": round(self.duration_s, 2),
                "quarantined": self.quarantined,
                "ticks_per_s": round(self.ticks_per_s, 0)}


def replay(engine: Engine, ticks: Iterable[Tick], *,
           publish: bool = False,
           progress_every: int = 0,
           on_progress: Optional[Callable[[ReplayReport], None]] = None
           ) -> ReplayReport:
    """Drive an engine over historical ticks.

    `publish=False` (the default and the safe one) forces the engine into REPLAY
    health, which zeroes the conviction multiplier and blocks delivery. Signals
    are still journaled and graded -- that is the entire point -- they simply
    never reach a human as if they were live.
    """
    rep = ReplayReport()
    prev_state = engine.guardian.state
    if not publish:
        engine.guardian.state = Health.REPLAY

    seen_sessions: set = set()
    t0 = time.monotonic()
    try:
        for t in ticks:
            rep.ticks_in += 1
            out = engine.on_tick(t)
            rep.signals += len(out.get("published", []))
            rep.resolved += len(out.get("resolved", []))
            sd = session_date(t.t_ns)
            if sd not in seen_sessions:
                seen_sessions.add(sd)
                rep.sessions.append(sd)
            if progress_every and rep.ticks_in % progress_every == 0:
                rep.duration_s = time.monotonic() - t0
                rep.ticks_per_s = rep.ticks_in / max(rep.duration_s, 1e-9)
                if on_progress:
                    on_progress(rep)
    finally:
        if not publish:
            engine.guardian.state = prev_state

    rep.duration_s = time.monotonic() - t0
    rep.ticks_per_s = rep.ticks_in / max(rep.duration_s, 1e-9)
    rep.ticks_accepted = engine.normalizer.stats.accepted
    rep.quarantined = engine.normalizer.stats.quarantined
    rep.frames = engine.features.frames_built
    rep.final_state_hash = engine.state_hash()
    return rep


def bootstrap(archive_root: str | Path, engine: Engine, *,
              limit_days: Optional[int] = None,
              on_progress: Optional[Callable[[ReplayReport], None]] = None
              ) -> ReplayReport:
    """Cold start: replay every archived day, oldest first.

    Order matters. Cumulative indicators (ADL, OBV, VWAP) and the learner's
    rankings are path-dependent, so replaying days out of order would produce a
    prior that could never have existed.
    """
    root = Path(archive_root)
    files = sorted(root.rglob("ticks-*.jsonl"))
    if limit_days:
        files = files[-limit_days:]

    def _chain():
        for f in files:
            yield from JsonlFeed(f).stream()

    rep = replay(engine, _chain(), publish=False,
                 progress_every=250_000, on_progress=on_progress)
    for sd in rep.sessions:
        try:
            engine.close_session(sd, days_elapsed=1.0)
        except Exception:
            continue        # a malformed archived day must not abort the boot
    return rep


def verify_state(engine: Engine, archive_root: str | Path,
                 from_checkpoint: Optional[str] = None) -> Dict[str, object]:
    """Rebuild state from the archive and compare hashes against the live engine.

    This is the function Guardian rung 3 exists to call. A mismatch means the
    live engine's memory has diverged from what its own inputs imply -- at which
    point the correct action is to trust the log and discard the memory, never
    the reverse.
    """
    live_hash = engine.state_hash()
    shadow = Engine(EngineConfig(root=str(Path(engine.cfg.root) / "_verify"),
                                 intervals=engine.cfg.intervals,
                                 durable_ledger=False),
                    strategists={})       # no strategists: features only
    root = Path(archive_root)
    files = sorted(root.rglob("ticks-*.jsonl"))

    def _chain():
        for f in files:
            yield from JsonlFeed(f).stream()

    rep = replay(shadow, _chain(), publish=False)
    match = rep.final_state_hash == live_hash
    result = {
        "match": match,
        "live_hash": live_hash,
        "replay_hash": rep.final_state_hash,
        "ticks_replayed": rep.ticks_in,
        "verdict": ("state verified — live memory agrees with a deterministic "
                    "re-derivation from the immutable archive")
                   if match else
                   ("STATE DIVERGENCE — live memory does not match what its own "
                    "inputs imply. Trust the log: discard memory and rebuild."),
    }
    shadow.close()
    return result
