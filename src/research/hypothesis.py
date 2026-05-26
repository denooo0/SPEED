"""Pydantic schema for a forged Hypothesis.

Mirrors the data shape in `plan/edge_generation_workflow.md` §2 (the LLM forge
output) and the runner contract in `src/backtest/runner.py`, which consumes the
hypothesis as a dict. A Hypothesis is a *falsifiable* claim about an edge: it
must name a mechanism and pre-register the events that would kill it.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Lens = Literal["flow", "structure", "context", "intent"]
EdgeDirection = Literal["mean_revert", "momentum", "breakout", "fade"]


class EntryRule(BaseModel):
    trigger: str
    features_required: List[str] = Field(default_factory=list)


class ExitRule(BaseModel):
    stop: str
    target: str
    time_stop: Optional[str] = None


class Filters(BaseModel):
    session: List[str] = Field(default_factory=list)
    regime: List[str] = Field(default_factory=list)
    cot_state: Optional[str] = None


class ExpectedEdge(BaseModel):
    direction: EdgeDirection
    horizon_bars: int

    @field_validator("horizon_bars")
    @classmethod
    def _positive_horizon(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("expected_edge.horizon_bars must be positive")
        return v


class Hypothesis(BaseModel):
    """One falsifiable edge claim. The forge produces exactly one per call."""

    id: str
    parent_observation: Optional[str] = None
    thesis: str
    mechanism: str
    lenses: List[Lens]
    entry_rule: EntryRule
    exit_rule: ExitRule
    filters: Filters = Field(default_factory=Filters)
    features_used: List[str] = Field(default_factory=list)
    expected_edge: ExpectedEdge
    falsifiers: List[str]
    min_sample: int = 60

    @model_validator(mode="after")
    def _enforce_doctrine(self) -> "Hypothesis":
        # Law 2: no edge without a named mechanism.
        if len(self.mechanism.strip()) < 20:
            raise ValueError(
                "mechanism too thin — name who is trapped, doing what, and why "
                "the edge persists (Law 2: no edge without a named mechanism)"
            )
        # Law 4: falsification before fitting — at least one pre-registered death.
        if not self.falsifiers or all(not f.strip() for f in self.falsifiers):
            raise ValueError(
                "at least one falsifier is required (Law 4: a hypothesis with no "
                "pre-registered way to die is hope, not science)"
            )
        if not self.lenses:
            raise ValueError("at least one lens must be cited")
        if self.min_sample < 60:
            raise ValueError("min_sample must be >= 60 (promotion gate floor)")
        return self

    def to_runner_dict(self) -> Dict[str, object]:
        """Serialize to the dict shape `BacktestRunner.evaluate_hypothesis` consumes."""
        return self.model_dump(mode="json")


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def new_hypothesis_id(slug: str, now: Optional[float] = None) -> str:
    """Stable, sortable id: `H-<YYYY>-<slug>-<epoch>`.

    The slug is a short human hint (e.g. 'ldn-pdh-sweep'); the epoch suffix keeps
    ids unique when the same observation is forged more than once.
    """
    ts = time.gmtime(now if now is not None else time.time())
    clean = _SLUG_RE.sub("-", slug.strip().lower()).strip("-") or "hypo"
    return f"H-{ts.tm_year}-{clean}-{int(now if now is not None else time.time())}"
