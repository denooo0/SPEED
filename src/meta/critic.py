"""Adversarial critic for meta-loop proposed changes.

The critic is a SEPARATE LLM call whose only job is to argue against
the proposed change. It tries to find:
  - Regimes where the change would degrade performance
  - Worst-case overfitting interpretations of the evidence
  - Invariants the change breaks (doctrine laws, sizing constraints, etc.)
  - Whether the proposer cherry-picked the autopsy cluster

The critic's prompt is also FROZEN at deployment. The meta-loop cannot
modify the critic's reasoning frame.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal

from src.meta.proposer import ProposedChange

CriticVerdict = Literal[
    "no_objection",          # critic could not find a structural problem
    "minor_concern",         # critic flags risk but does not block
    "blocking_objection",    # critic identifies an unmitigated failure mode
]


@dataclass
class CritiqueResult:
    change_id: str
    verdict: CriticVerdict
    objections: List[str]            # bullet points the critic raised
    proposer_responses: List[str]    # if the proposer was given a chance to reply
    critic_model: str
    critic_prompt_hash: str


class AdversarialCritic:
    """Runs a second LLM call that argues AGAINST the proposed change.

    A `blocking_objection` verdict prevents deployment regardless of
    backtest results. This is the design's anti-Goodhart firewall.
    """

    def critique(
        self,
        change: ProposedChange,
        oracle_result_summary: str,
    ) -> CritiqueResult:
        raise NotImplementedError
