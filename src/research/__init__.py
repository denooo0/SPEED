"""Edge research: forge raw observations into falsifiable, backtest-ready hypotheses.

The front of the edge-generation pipeline (`plan/edge_generation_workflow.md`),
governed by `prompts/ATLAS_RESEARCH_PARTNER.md`.
"""
from src.research.hypothesis import (
    EntryRule,
    ExitRule,
    ExpectedEdge,
    Filters,
    Hypothesis,
    new_hypothesis_id,
)
from src.research.hypothesis_forge import ForgeConfig, HypothesisForge
from src.research.store import HypothesisStore

__all__ = [
    "EntryRule",
    "ExitRule",
    "ExpectedEdge",
    "Filters",
    "Hypothesis",
    "new_hypothesis_id",
    "ForgeConfig",
    "HypothesisForge",
    "HypothesisStore",
]
