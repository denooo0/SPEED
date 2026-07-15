"""Track F — Observability + experiment logging.

Layer components:

* :mod:`src.experiments.registry` — every hypothesis ever tested lives here.
  Feeds the accurate ``n_trials`` count into the Deflated Sharpe Ratio (DSR)
  calculation, closing the family-wise multiple-testing loophole.
* :mod:`src.experiments.logger` — one JSON per experiment on disk, so the
  entire history of a scientific run can be replayed / audited.
* :mod:`src.experiments.drift` — Wilson-CI + KS-test based comparison of the
  live trading distribution vs. the reference backtest distribution. Emits a
  ``healthy | drifting | broken`` verdict per the operational-policies spec.

This subtree is owned by Track F; nothing else should mutate ``experiments/``
on disk without going through these classes.
"""
from __future__ import annotations

from src.experiments.drift import DriftDetector, DriftReport
from src.experiments.logger import Experiment, ExperimentLogger
from src.experiments.registry import HypothesisEntry, HypothesisRegistry

__all__ = [
    "DriftDetector",
    "DriftReport",
    "Experiment",
    "ExperimentLogger",
    "HypothesisEntry",
    "HypothesisRegistry",
]
