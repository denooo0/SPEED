"""The Guardian: health state machine + heartbeats.

Owns the single question "can this engine be trusted to publish right now?" and
enforces the answer. Every transition is reasoned, logged, and reversible, and
recovery never returns straight to HEALTHY -- it returns to DEGRADED probation,
because a system that just healed has not yet earned trust.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.types import Health
from .detectors import Metrics, Severity, Verdict, run_all, worst
from .recovery import Recovery, RepairResult


@dataclass
class Transition:
    t_ns: int
    frm: Health
    to: Health
    trigger: str
    reason: str
    verdicts: List[str] = field(default_factory=list)


@dataclass
class Heartbeat:
    component: str
    last_beat_ns: int = 0
    timeout_s: float = 30.0
    beats: int = 0

    def beat(self, t_ns: int) -> None:
        self.last_beat_ns = t_ns
        self.beats += 1

    def stale(self, now_ns: int) -> bool:
        if self.last_beat_ns == 0:
            return False
        return (now_ns - self.last_beat_ns) / 1e9 > self.timeout_s


class Guardian:
    def __init__(self, recovery: Optional[Recovery] = None,
                 probation_checks: int = 5,
                 repair_cooldown_s: float = 900.0,
                 max_repairs_per_fault: int = 3,
                 max_recovering_checks: int = 20):
        self.state = Health.HEALTHY
        self.recovery = recovery
        self.transitions: List[Transition] = []
        self.heartbeats: Dict[str, Heartbeat] = {}
        self.last_verdicts: List[Verdict] = []
        self.probation_checks = probation_checks
        self._clean_streak = 0
        self._incident_seq = 0
        self.on_incident: Optional[Callable[[Dict[str, Any]], None]] = None

        # --- repair-storm control -------------------------------------
        # A persistent fault must not cause the ladder to be re-climbed on
        # every health cycle. Repairing a fault that repair cannot fix is
        # pure cost: it burns I/O, floods the incident journal, and hides the
        # ONE incident that mattered among hundreds of identical ones.
        # After `max_repairs_per_fault` attempts inside the cooldown window,
        # the honest conclusion is "self-repair cannot fix this" -> SAFE.
        self.repair_cooldown_s = repair_cooldown_s
        self.max_repairs_per_fault = max_repairs_per_fault
        self._last_repair_ns: Dict[str, int] = {}
        self._repair_attempts: Dict[str, int] = {}
        self.suppressed_repairs: Dict[str, int] = {}
        self.max_recovering_checks = max_recovering_checks
        self._recovering_checks = 0

    # ---- heartbeats ------------------------------------------------------

    def register_component(self, name: str, timeout_s: float = 30.0) -> None:
        self.heartbeats[name] = Heartbeat(name, timeout_s=timeout_s)

    def beat(self, name: str, t_ns: int) -> None:
        if name in self.heartbeats:
            self.heartbeats[name].beat(t_ns)

    def stale_components(self, now_ns: int) -> List[str]:
        return [n for n, h in self.heartbeats.items() if h.stale(now_ns)]

    # ---- the cycle -------------------------------------------------------

    def assess(self, t_ns: int, m: Metrics) -> Dict[str, Any]:
        """One health cycle: detect -> decide -> (repair) -> report."""
        verdicts = run_all(m)
        self.last_verdicts = verdicts
        sev = worst(verdicts)

        stale = self.stale_components(t_ns)
        if stale:
            sev = max(sev, Severity.CRITICAL)

        target = self._target_state(sev)
        report: Dict[str, Any] = {
            "t_ns": t_ns, "state": self.state.value, "severity": int(sev),
            "verdicts": [v.detector for v in verdicts],
            "stale_components": stale, "repairs": [],
            "publishing": False, "conviction_multiplier": 1.0,
        }

        if target is not self.state:
            trigger = verdicts[0].detector if verdicts else (
                f"stale:{stale[0]}" if stale else "clean")
            reason = verdicts[0].description if verdicts else "components healthy"
            self._transition(t_ns, target, trigger, reason,
                             [v.detector for v in verdicts])

            if target is Health.RECOVERING and self.recovery is not None:
                crit = next((v for v in verdicts
                             if v.severity >= Severity.CRITICAL), None)
                fault = crit.detector if crit else "unknown"
                allowed, why = self._may_repair(fault, t_ns)
                if not allowed:
                    self.suppressed_repairs[fault] = \
                        self.suppressed_repairs.get(fault, 0) + 1
                    report["repair_suppressed"] = why
                    if self._repair_attempts.get(fault, 0) >= self.max_repairs_per_fault:
                        # Repair has been tried and the fault persists. Looping
                        # is not resilience, it is denial. Stop and escalate.
                        self._transition(t_ns, Health.SAFE, "repair_exhausted",
                                         f"{fault} survived "
                                         f"{self._repair_attempts[fault]} repair "
                                         f"attempts; self-healing cannot fix it")
                        self._emit_incident(t_ns, crit, [], escalated=True)
                else:
                    self._repair_attempts[fault] = \
                        self._repair_attempts.get(fault, 0) + 1
                    self._last_repair_ns[fault] = t_ns
                    results = self.recovery.repair(
                        detector=fault,
                        start_rung=(crit.auto_fix if crit and crit.auto_fix in
                                    ("rollback_params", "rollback_state",
                                     "replay_rebuild", "rollback_roster", "halt")
                                    else None))
                    report["repairs"] = [
                        {"rung": r.rung, "ok": r.success, "verified": r.verified,
                         "detail": r.detail} for r in results]
                    healed = self.recovery.suggested_state(results)
                    self._transition(t_ns, healed, "repair_complete",
                                     f"climbed {len(results)} rung(s)")
                    self._emit_incident(t_ns, crit, results)

        # --- stuck-in-recovery watchdog -------------------------------
        # If the engine sits in RECOVERING without healing, it is not
        # publishing and nobody has been told. A system that fails silently
        # and indefinitely is worse than one that stops loudly, so after
        # `max_recovering_checks` it escalates to SAFE and demands a human.
        if self.state is Health.RECOVERING:
            self._recovering_checks += 1
            if self._recovering_checks >= self.max_recovering_checks:
                self._transition(t_ns, Health.SAFE, "recovery_stalled",
                                 f"remained in RECOVERING for "
                                 f"{self._recovering_checks} consecutive checks "
                                 f"without healing; escalating rather than "
                                 f"stalling silently")
                self._emit_incident(t_ns, verdicts[0] if verdicts else None,
                                    [], escalated=True)
                self._recovering_checks = 0
        else:
            self._recovering_checks = 0

        # probation: several consecutive clean cycles to earn HEALTHY back
        if self.state is Health.DEGRADED:
            if sev <= Severity.INFO and not stale:
                self._clean_streak += 1
                if self._clean_streak >= self.probation_checks:
                    self._transition(t_ns, Health.HEALTHY, "probation_passed",
                                     f"{self._clean_streak} clean checks")
                    self._clean_streak = 0
            else:
                self._clean_streak = 0

        report["state"] = self.state.value
        report["publishing"] = self.can_publish()
        report["conviction_multiplier"] = self.conviction_multiplier()
        return report

    def _may_repair(self, fault: str, t_ns: int) -> tuple:
        """Rate-limit repair per fault signature.

        A clean window resets the attempt counter -- the fault genuinely went
        away and a fresh recurrence deserves a fresh repair. Inside the window,
        repeated attempts are suppressed and counted.
        """
        last = self._last_repair_ns.get(fault, 0)
        if last and (t_ns - last) / 1e9 > self.repair_cooldown_s:
            self._repair_attempts[fault] = 0        # window elapsed: forgive
            return True, ""
        attempts = self._repair_attempts.get(fault, 0)
        if attempts >= self.max_repairs_per_fault:
            return False, (f"{fault} already repaired {attempts}x within "
                           f"{self.repair_cooldown_s:.0f}s and persists")
        if last and (t_ns - last) / 1e9 < 60.0:
            return False, (f"{fault} repaired {(t_ns-last)/1e9:.0f}s ago; "
                           f"waiting before re-attempting")
        return True, ""

    def _target_state(self, sev: Severity) -> Health:
        if self.state is Health.SAFE:
            return Health.SAFE            # only an operator clears SAFE
        if sev >= Severity.FATAL:
            return Health.SAFE
        if sev >= Severity.CRITICAL:
            return Health.RECOVERING
        if sev >= Severity.WARN:
            return Health.DEGRADED
        return Health.DEGRADED if self.state is Health.DEGRADED else Health.HEALTHY

    def _transition(self, t_ns: int, to: Health, trigger: str, reason: str,
                    verdicts: Optional[List[str]] = None) -> None:
        if to is self.state:
            return
        self.transitions.append(
            Transition(t_ns, self.state, to, trigger, reason, verdicts or []))
        self.state = to

    def _emit_incident(self, t_ns: int, crit: Optional[Verdict],
                       results: List[RepairResult],
                       escalated: bool = False) -> None:
        if self.on_incident is None:
            return
        self._incident_seq += 1
        last = results[-1] if results else None
        self.on_incident({
            "seq": self._incident_seq, "t_ns": t_ns,
            "detector": crit.detector if crit else "unknown",
            "severity": crit.severity.name if crit else "CRITICAL",
            "description": crit.description if crit else "",
            "prescribed_fix": crit.fix if crit else "",
            "from_state": Health.RECOVERING.value, "to_state": self.state.value,
            "repair_ladder": [f"{r.rung}: {'ok' if r.success else 'failed'} "
                              f"({r.detail})" for r in results],
            "verification": (f"replay hash {last.state_hash_after[:12]} "
                             f"{'MATCHES' if last.verified else 'DID NOT MATCH'} "
                             f"expected {last.state_hash_before[:12]}")
                            if last else "no repair attempted",
            "resolved": bool(last and last.success and last.verified),
            "held": self.state is not Health.SAFE,
            "escalated": escalated,
            "repair_attempts": self._repair_attempts.get(
                crit.detector if crit else "unknown", 0),
            "resolution_note": (
                (f"ESCALATED: self-repair attempted "
                 f"{self._repair_attempts.get(crit.detector if crit else '', 0)}x "
                 f"and the fault persisted. Looping is denial, not resilience — "
                 f"stopped and handed to a human.") if escalated else
                ("Returned to DEGRADED probation; must pass "
                 f"{self.probation_checks} clean checks before full publishing "
                 "resumes.") if self.state is Health.DEGRADED else
                "Escalated to SAFE — human review required."),
        })

    # ---- gates the rest of the engine obeys ------------------------------

    def can_publish(self) -> bool:
        return self.state in (Health.HEALTHY, Health.DEGRADED)

    def conviction_multiplier(self) -> float:
        """Degraded states publish smaller, not louder. Uncertainty tightens
        risk; it never loosens it."""
        return {Health.HEALTHY: 1.0, Health.DEGRADED: 0.6,
                Health.RECOVERING: 0.0, Health.SAFE: 0.0,
                Health.REPLAY: 0.0}[self.state]

    def clear_safe(self, t_ns: int, operator_note: str) -> None:
        """Only a human clears SAFE. Deliberate friction."""
        if self.state is Health.SAFE:
            self._transition(t_ns, Health.DEGRADED, "operator_clear", operator_note)
            self._clean_streak = 0

    def summary(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "transitions": len(self.transitions),
            "recent": [{"from": t.frm.value, "to": t.to.value,
                        "trigger": t.trigger} for t in self.transitions[-5:]],
            "active_verdicts": [
                {"detector": v.detector, "severity": v.severity.name,
                 "fix": v.fix} for v in self.last_verdicts],
        }
