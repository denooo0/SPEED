"""Live self-diagnosis: the engine watching itself.

Each detector is a pure function of observed metrics -> a verdict with a
PRESCRIBED FIX ATTACHED. That last part is the design point: a detector that
only reports is a dashboard, and dashboards require a human to be awake. A
detector that carries its remedy lets the Guardian act at 3am.

Severity drives the health state machine:
    INFO     -> note it
    WARN     -> DEGRADED (reduce conviction, widen gates)
    CRITICAL -> RECOVERING (stop publishing, climb the repair ladder)
    FATAL    -> SAFE (full stop, human required)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional


class Severity(IntEnum):
    OK = 0
    INFO = 1
    WARN = 2
    CRITICAL = 3
    FATAL = 4


@dataclass(frozen=True)
class Verdict:
    detector: str
    severity: Severity
    value: float
    threshold: float
    description: str
    fix: str
    auto_fix: Optional[str] = None   # repair-ladder rung the Guardian may climb


@dataclass
class Metrics:
    """Everything the detectors observe. Populated by the engine each cycle."""
    # feed
    p99_inter_arrival_ms: float = 0.0
    gaps_per_hour: float = 0.0
    critical_gaps: int = 0
    quarantine_rate: float = 0.0
    clock_skew_ms: float = 0.0
    # pipeline
    frame_latency_ms: float = 0.0
    stale_proposal_rate: float = 0.0
    queue_depth: int = 0
    # learning
    # `calibration_n` gates every calibration-derived detector. Declaring
    # "calibration collapse" on a dozen samples is not diagnosis, it is noise --
    # and a detector that cries wolf on thin data triggers repair storms that
    # cost far more than the fault would have.
    calibration_n: int = 0
    min_calibration_n: int = 50
    brier_score: float = 0.25
    calibration_drift: float = 0.0
    rolling_win_rate: float = 0.5
    mechanism_false_rate: float = 0.0
    inter_seat_correlation: float = 0.0
    # production
    signals_per_day_7d: float = 5.0
    exploration_rate: float = 0.07
    # cost / risk
    daily_cost_usd: float = 0.0
    cost_budget_usd: float = 50.0
    drawdown_r: float = 0.0
    max_drawdown_r: float = 20.0
    # integrity
    state_hash_match: bool = True
    schema_version_conflicts: int = 0
    wal_truncations: int = 0


def _v(name, sev, val, thr, desc, fix, auto=None) -> Verdict:
    return Verdict(name, sev, val, thr, desc, fix, auto)


DetectorFn = Callable[[Metrics], Optional[Verdict]]
REGISTRY: Dict[str, DetectorFn] = {}


def detector(name: str):
    def wrap(fn: DetectorFn):
        REGISTRY[name] = fn
        return fn
    return wrap


# ---- 1. feed health --------------------------------------------------------

@detector("feed_latency")
def _feed_latency(m: Metrics):
    if m.p99_inter_arrival_ms > 30_000:
        return _v("feed_latency", Severity.CRITICAL, m.p99_inter_arrival_ms, 30_000,
                  "Feed effectively silent; reasoning would be on stale tape.",
                  "Fail over to secondary feed; pause emissions until fresh.",
                  auto="failover_feed")
    if m.p99_inter_arrival_ms > 5_000:
        return _v("feed_latency", Severity.WARN, m.p99_inter_arrival_ms, 5_000,
                  "Tick p99 inter-arrival elevated.",
                  "Reduce conviction; widen gates until arrival normalizes.")
    return None


@detector("feed_gaps")
def _feed_gaps(m: Metrics):
    if m.critical_gaps > 0:
        return _v("feed_gaps", Severity.CRITICAL, m.critical_gaps, 0,
                  "Critical data gap: indicators are cumulative, so a gap "
                  "silently biases ADL/VWAP for the rest of the session.",
                  "Quarantine window, replay-rebuild from last checkpoint.",
                  auto="replay_rebuild")
    if m.gaps_per_hour > 6:
        return _v("feed_gaps", Severity.WARN, m.gaps_per_hour, 6,
                  "Frequent small gaps.", "Check connectivity; mark data LATE.")
    return None


@detector("data_quality")
def _data_quality(m: Metrics):
    if m.quarantine_rate > 0.05:
        return _v("data_quality", Severity.CRITICAL, m.quarantine_rate, 0.05,
                  ">5% of ticks failing sanity checks — the feed is unreliable.",
                  "Stop publishing; verify source; failover.", auto="failover_feed")
    if m.quarantine_rate > 0.01:
        return _v("data_quality", Severity.WARN, m.quarantine_rate, 0.01,
                  "Elevated tick quarantine rate.", "Inspect quarantine reasons.")
    return None


@detector("clock_skew")
def _clock_skew(m: Metrics):
    if abs(m.clock_skew_ms) > 2000:
        return _v("clock_skew", Severity.WARN, m.clock_skew_ms, 2000,
                  "Broker/local clock disagreement large enough to mis-bucket bars.",
                  "Re-sync NTP; prefer broker time for bar boundaries.")
    return None


# ---- 2. pipeline health ----------------------------------------------------

@detector("stale_proposals")
def _stale(m: Metrics):
    if m.stale_proposal_rate > 0.25:
        return _v("stale_proposals", Severity.CRITICAL, m.stale_proposal_rate, 0.25,
                  "A quarter of proposals miss their frame deadline — the engine "
                  "is reasoning about tape that has already moved.",
                  "Tighten pre-filter, batch inference, or scale seats down.",
                  auto="tighten_prefilter")
    if m.stale_proposal_rate > 0.10:
        return _v("stale_proposals", Severity.WARN, m.stale_proposal_rate, 0.10,
                  "Proposals arriving late.", "Raise pre-filter threshold.")
    return None


@detector("backpressure")
def _backpressure(m: Metrics):
    if m.queue_depth > 200:
        return _v("backpressure", Severity.CRITICAL, m.queue_depth, 200,
                  "Inference queue saturated; latency will compound.",
                  "Shed load: raise pre-filter threshold, drop lowest-rank seat.",
                  auto="tighten_prefilter")
    if m.queue_depth > 50:
        return _v("backpressure", Severity.WARN, m.queue_depth, 50,
                  "Queue building.", "Monitor; prepare to shed load.")
    return None


# ---- 3. learning health ----------------------------------------------------

@detector("calibration_collapse")
def _calib(m: Metrics):
    # Brier > 0.25 is worse than always predicting 50% -- the model's confidence
    # is actively misleading, which is worse than having no model.
    if m.calibration_n < m.min_calibration_n:
        return None          # not enough evidence to accuse anyone
    if m.brier_score > 0.30:
        return _v("calibration_collapse", Severity.CRITICAL, m.brier_score, 0.30,
                  "Conviction numbers are worse than uninformative. Confident "
                  "wrong calls corrupt every downstream lesson.",
                  "Roll back parameter policy + seat weights to last verified "
                  "snapshot; re-enter probation.", auto="rollback_params")
    if m.brier_score > 0.25:
        return _v("calibration_collapse", Severity.WARN, m.brier_score, 0.25,
                  "Calibration degrading.", "Apply isotonic recalibration layer.")
    return None


@detector("regime_break")
def _regime(m: Metrics):
    if m.calibration_n < m.min_calibration_n:
        return None
    if m.calibration_drift > 0.12:
        return _v("regime_break", Severity.WARN, m.calibration_drift, 0.12,
                  "Calibration decaying steadily — the market regime that "
                  "trained these seats is likely gone.",
                  "Re-condition on current regime; down-weight stale lessons; "
                  "revert to conservative parameters.", auto="rollback_params")
    return None


@detector("mechanism_rot")
def _mech(m: Metrics):
    if m.calibration_n < m.min_calibration_n:
        return None
    if m.mechanism_false_rate > 0.45:
        return _v("mechanism_rot", Severity.WARN, m.mechanism_false_rate, 0.45,
                  "Seats are being right for the wrong reasons — profits without "
                  "valid mechanisms. This looks fine on a P&L and rots the learner.",
                  "Increase mechanism penalty in reward; audit top seat's theses.")
    return None


@detector("consensus_collapse")
def _consensus(m: Metrics):
    if m.inter_seat_correlation > 0.85:
        return _v("consensus_collapse", Severity.WARN, m.inter_seat_correlation, 0.85,
                  "Seats have converged into one voice — the Colosseum is now "
                  "theatre and the anti-laziness property is gone.",
                  "Force-mutate a lens; raise reward for distinct-correct calls.",
                  auto="mutate_lens")
    return None


# ---- 4. production health --------------------------------------------------

@detector("signal_drought")
def _drought(m: Metrics):
    if m.signals_per_day_7d < 3.0:
        return _v("signal_drought", Severity.WARN, m.signals_per_day_7d, 3.0,
                  "Well under the 5/day target on a 7-day mean.",
                  "Diagnose: too-strict gates, dead regime, or genuinely thin "
                  "market. Tune pre-filter SENSITIVITY only — never the quality "
                  "bar. Forcing signals is a punished failure mode.")
    return None


@detector("exploration_starved")
def _explore(m: Metrics):
    if m.exploration_rate < 0.02:
        return _v("exploration_starved", Severity.INFO, m.exploration_rate, 0.02,
                  "Almost no exploration — the learner will plateau at a local "
                  "optimum and look healthy while doing it.",
                  "Restore exploration budget to 5-10% of signals.")
    return None


# ---- 5. cost & risk --------------------------------------------------------

@detector("cost_budget")
def _cost(m: Metrics):
    r = m.daily_cost_usd / m.cost_budget_usd if m.cost_budget_usd else 0
    if r > 1.0:
        return _v("cost_budget", Severity.CRITICAL, r, 1.0,
                  "Daily compute budget exhausted.",
                  "Degrade gracefully: raise pre-filter threshold so the engine "
                  "gets pickier rather than going dark.", auto="tighten_prefilter")
    if r > 0.8:
        return _v("cost_budget", Severity.WARN, r, 0.8,
                  "80% of daily budget consumed.", "Begin tightening pre-filter.")
    return None


@detector("drawdown")
def _dd(m: Metrics):
    if m.drawdown_r >= m.max_drawdown_r:
        return _v("drawdown", Severity.FATAL, m.drawdown_r, m.max_drawdown_r,
                  "Drawdown limit breached.",
                  "FULL STOP. Human review required before any further signal.",
                  auto="halt")
    if m.drawdown_r > m.max_drawdown_r * 0.7:
        return _v("drawdown", Severity.WARN, m.drawdown_r, m.max_drawdown_r * 0.7,
                  "Approaching drawdown limit.", "Halve size guidance.")
    return None


# ---- 6. integrity ----------------------------------------------------------

@detector("state_divergence")
def _state(m: Metrics):
    if not m.state_hash_match:
        return _v("state_divergence", Severity.CRITICAL, 0, 1,
                  "Live state hash disagrees with deterministic replay — the "
                  "engine cannot currently trust its own memory.",
                  "Discard in-memory state; replay-rebuild from last checkpoint; "
                  "verify before resuming.", auto="replay_rebuild")
    return None


@detector("schema_conflict")
def _schema(m: Metrics):
    if m.schema_version_conflicts > 0:
        return _v("schema_conflict", Severity.CRITICAL, m.schema_version_conflicts, 0,
                  "Mixed feature-schema versions in the training set — the "
                  "learner would compare apples to a redefined orange.",
                  "Quarantine old-version rows; migrate or exclude.",
                  auto="quarantine_schema")
    return None


@detector("wal_truncation")
def _wal(m: Metrics):
    if m.wal_truncations > 0:
        return _v("wal_truncation", Severity.INFO, m.wal_truncations, 0,
                  "WAL torn tail was truncated on open (normal after a crash).",
                  "No action: idempotent writes re-append any lost record.")
    return None


def run_all(m: Metrics) -> List[Verdict]:
    out = [v for fn in REGISTRY.values() if (v := fn(m)) is not None]
    return sorted(out, key=lambda x: -x.severity)


def worst(verdicts: List[Verdict]) -> Severity:
    return max((v.severity for v in verdicts), default=Severity.OK)
