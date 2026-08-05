"""The Runner: the 24/7 supervised process.

Responsibilities the Engine deliberately does NOT have, so that the reasoning
core stays deterministic and testable:

  * wall-clock concerns (housekeeping cadence, session rollover, uptime)
  * OS signals and graceful shutdown
  * restart-from-checkpoint on boot
  * cost governance (getting pickier as budget depletes, rather than going dark)
  * Telegram draining and inbound Taken/Skipped polling
  * crash containment -- an exception in one tick must not kill the process

The design rule throughout: **the tape never stops because a side-effect
failed.** Delivery, journaling flushes, and retrospectives are all best-effort;
ingestion and grading are not.
"""
from __future__ import annotations

import signal
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import Config
from .core.clock import NS, session_date
from .core.types import Health, Tick
from .delivery.telegram import TelegramPublisher
from .engine import Engine, EngineConfig
from .feeds.base import Feed, TickArchiver
from .journal.retro import Retrospective


@dataclass
class RunnerStats:
    started_ns: int = 0
    ticks: int = 0
    signals: int = 0
    resolved: int = 0
    errors: int = 0
    sessions_closed: int = 0
    restarts: int = 0
    last_error: str = ""
    uptime_s: float = 0.0


class Runner:
    def __init__(self, cfg: Config, engine: Engine, feed: Feed, *,
                 publisher: Optional[TelegramPublisher] = None,
                 archiver: Optional[TickArchiver] = None,
                 now: Optional[Callable[[], float]] = None):
        self.cfg = cfg
        self.engine = engine
        self.feed = feed
        self.publisher = publisher
        self.archiver = archiver
        self.stats = RunnerStats()
        self._now = now or time.monotonic
        self._stop = False
        self._current_session = ""
        self._last_housekeeping = 0.0
        self._tg_offset = 0
        self._day_cost_usd = 0.0
        self._cost_day = ""
        self.log: Callable[[str], None] = lambda m: print(m, flush=True)

    # ---- lifecycle -------------------------------------------------------

    def install_signal_handlers(self) -> None:
        def handler(signum, frame):
            self.log(f"[runner] signal {signum} received — shutting down cleanly")
            self.stop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, handler)
            except (ValueError, OSError):
                pass          # not the main thread, or unsupported platform

    def stop(self) -> None:
        self._stop = True
        if hasattr(self.feed, "stop"):
            self.feed.stop()

    def boot(self) -> None:
        """Restart-from-checkpoint. State is rebuilt from durable artifacts, not
        assumed -- a process that boots into stale in-memory defaults is exactly
        how a restart silently changes behaviour."""
        cp = self.engine.registry.last_verified()
        if cp is None:
            self.log("[runner] no verified checkpoint — cold boot")
            return
        try:
            self.engine.bandit.restore(cp.payload.get("bandit", {}))
            self.engine._signal_seq = int(cp.payload.get("signal_seq", 0))
            self.stats.restarts += 1
            self.log(f"[runner] restored from {cp.checkpoint_id} "
                     f"(state {cp.state_hash[:12]}, seq {self.engine._signal_seq})")
            chain = self.engine.ledger.verify_chain()
            if not chain.get("ok"):
                self.log(f"[runner] LEDGER CHAIN BROKEN at {chain.get('broken_at')} "
                         f"— entering SAFE, human review required")
                self.engine.guardian.state = Health.SAFE
        except Exception as e:
            self.log(f"[runner] checkpoint restore failed ({e}); cold boot instead")

    # ---- main loop -------------------------------------------------------

    def run(self) -> RunnerStats:
        self.install_signal_handlers()
        self.boot()
        self.stats.started_ns = int(time.time() * NS)
        t_start = self._now()
        self.log(f"[runner] streaming {self.cfg.symbol} — "
                 f"health={self.engine.guardian.state.value}")

        try:
            for tick in self.feed.stream():
                if self._stop:
                    break
                self._on_tick(tick)
        except KeyboardInterrupt:
            self.log("[runner] interrupted")
        except Exception:
            self.stats.errors += 1
            self.stats.last_error = traceback.format_exc(limit=3)
            self.log(f"[runner] FATAL feed error:\n{self.stats.last_error}")
        finally:
            self.shutdown()

        self.stats.uptime_s = self._now() - t_start
        return self.stats

    def _on_tick(self, tick: Tick) -> None:
        self.stats.ticks += 1
        sd = session_date(tick.t_ns)

        # session rollover: close the book, write retros, reset daily cost
        if self._current_session and sd != self._current_session:
            self._roll_session(self._current_session)
        self._current_session = sd
        if self._cost_day != sd:
            self._cost_day, self._day_cost_usd = sd, 0.0

        if self.archiver:
            try:
                self.archiver.write(tick, sd)
            except Exception as e:
                self.stats.last_error = f"archiver: {e}"   # never fatal

        try:
            out = self.engine.on_tick(tick)
        except Exception:
            # One bad tick must not kill a 24/7 process. Count it, journal it,
            # keep the tape moving; the Guardian sees the error rate.
            self.stats.errors += 1
            self.stats.last_error = traceback.format_exc(limit=3)
            if self.stats.errors % 50 == 1:
                self.log(f"[runner] tick error ({self.stats.errors} total):\n"
                         f"{self.stats.last_error}")
            return

        for sid in out.get("published", []):
            self.stats.signals += 1
        self.stats.resolved += len(out.get("resolved", []))
        self._deliver(out, sd)

        if self._now() - self._last_housekeeping >= self.cfg.housekeeping_every_s:
            self._last_housekeeping = self._now()
            self._housekeeping()

    # ---- delivery --------------------------------------------------------

    def _deliver(self, out: Dict[str, object], sd: str) -> None:
        """Journal-first, send-second: by the time we get here the signal is
        already durable, so a delivery failure costs a notification, not a record."""
        if not self.publisher:
            return
        try:
            for sid in out.get("published_ids", []) or []:
                recs = self.engine.ledger.get(str(sid), "signal")
                if recs:
                    self.publisher.publish_signal(recs[0]["payload"])
            if self.cfg.telegram_send_outcomes:
                for sid in out.get("resolved", []) or []:
                    recs = self.engine.ledger.get(f"OUT-{sid}", "outcome")
                    if recs:
                        self.publisher.publish_outcome(str(sid), recs[0]["payload"])
        except Exception as e:
            self.stats.last_error = f"delivery: {e}"

    # ---- periodic --------------------------------------------------------

    def _housekeeping(self) -> None:
        if self.publisher:
            try:
                self.publisher.drain(max_items=20)
                for cb in self.publisher.poll_callbacks(self._tg_offset):
                    self._tg_offset = max(self._tg_offset,
                                          int(cb.get("update_id", 0)) + 1)
                    self._record_human_tag(cb["signal_id"], cb["action"])
            except Exception as e:
                self.stats.last_error = f"telegram housekeeping: {e}"
        self._govern_cost()

    def _record_human_tag(self, sid: str, action: str) -> None:
        """Your Taken/Skipped taps become a second opinion the learner can be
        measured against: where did we disagree, and who was right?"""
        try:
            self.engine.ledger.append(
                "human_tag", f"TAG-{sid}", self.engine._t_ns,
                session_date(self.engine._t_ns),
                {"signal_id": sid, "action": action})
            self.engine.journal.event(session_date(self.engine._t_ns),
                                      {"kind": "human_tag", "signal_id": sid,
                                       "action": action})
        except Exception as e:
            self.stats.last_error = f"human tag: {e}"

    def _govern_cost(self) -> None:
        """Degrade gracefully: get PICKIER as budget depletes rather than going
        dark. A silent engine and a broke engine look the same from outside, so
        the response is a raised bar, not a shutdown."""
        spent = sum(getattr(s, "stats", None).est_cost_usd
                    for s in self.engine.strategists.values()
                    if getattr(s, "stats", None)) if self.engine.strategists else 0.0
        self._day_cost_usd = spent
        budget = self.cfg.daily_cost_budget_usd
        if budget <= 0:
            return
        ratio = spent / budget
        if ratio >= 1.0:
            bump = 0.30
        elif ratio >= 0.8:
            bump = 0.15
        else:
            bump = 0.0
        if bump:
            for pf in self.engine.prefilters:
                pf.threshold = min(0.95, 0.5 + bump)
            if ratio >= 1.0 and self.stats.ticks % 10000 == 0:
                self.log(f"[runner] cost budget exhausted (${spent:.2f}/"
                         f"${budget:.2f}) — pre-filters tightened, not disabled")
        else:
            for pf in self.engine.prefilters:
                pf.threshold = 0.5

    def _roll_session(self, sd: str) -> None:
        try:
            p = self.engine.close_session(sd, days_elapsed=1.0)
            self.stats.sessions_closed += 1
            self.log(f"[runner] session {sd} closed -> {p}")
            retro = Retrospective(self.engine.journal)
            if retro.weekly():
                self.log("[runner] weekly retrospective written")
            if sd.endswith("-01") and retro.monthly():
                self.log("[runner] monthly retrospective written")
            if self.publisher:
                self.publisher.publish_text(
                    f"📓 <b>Session {sd} closed</b>\n"
                    f"Signals {int(self.engine._session_stats.get(sd, {}).get('signals', 0))} · "
                    f"Net R {self.engine._session_stats.get(sd, {}).get('r', 0):+.2f}")
        except Exception as e:
            self.stats.last_error = f"session roll: {e}"
            self.log(f"[runner] session roll failed: {e}")

    def shutdown(self) -> None:
        self.log("[runner] shutting down — flushing durable state")
        try:
            self.engine.checkpoint()
        except Exception:
            pass
        if self._current_session:
            try:
                self.engine.close_session(self._current_session)
            except Exception:
                pass
        if self.archiver:
            self.archiver.close()
        if self.publisher:
            try:
                self.publisher.drain(max_items=100)
            except Exception:
                pass
        try:
            self.engine.close()
        except Exception:
            pass
        self.log(f"[runner] done — {self.stats.ticks:,} ticks, "
                 f"{self.stats.signals} signals, {self.stats.errors} errors")
