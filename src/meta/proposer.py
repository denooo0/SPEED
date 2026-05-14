"""Meta-proposer interface.

Reads the digest + clustered autopsies + current configs/prompts, emits
a list of ProposedChange objects. Each change has tier (A/B/C), target
(config field / prompt section / rule), diff, and rationale.

The proposer's own system prompt is FROZEN at deployment. The proposer
cannot modify itself (Tier D forbidden). Only the operator edits this
file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

Tier = Literal["A", "B", "C"]
ChangeTarget = Literal[
    "config_threshold",      # GateConfig values, signal interval, etc.
    "validate_take_rule",    # additional rejection clause
    "lens_weight",           # confidence_breakdown reweighting
    "setup_file",            # new memory/setups/<name>.md
    "addendum_section",      # addition to prompts/ATLAS_ADDENDUM.md
    "confidence_calibration",  # Platt scaling table
]


@dataclass
class ProposedChange:
    change_id: str           # uuid
    tier: Tier
    target: ChangeTarget
    rationale: str           # human-readable why
    evidence: List[str]      # autopsy IDs / digest excerpts cited
    diff: Dict[str, Any]     # the actual change (e.g. {"path": ..., "before": ..., "after": ...})
    proposed_at: int         # ms epoch
    proposer_model: str      # "claude-opus-4-7", "qwen3-max", etc.
    proposer_prompt_hash: str  # hash of the frozen proposer system prompt


class MetaProposer:
    """Interface only — implementations live in proposer_claude.py / proposer_qwen.py."""

    def propose(
        self,
        digest_md: str,
        autopsy_cluster: List[Dict[str, Any]],
        current_config: Dict[str, Any],
        current_addendum: str,
        active_tiers: List[Tier],
    ) -> List[ProposedChange]:
        """Generate ProposedChange[] for the active tiers.

        Implementations must respect rate limits (max 3/tier/week, see
        plan/meta_learning_loop.md §IV safety rail 3).
        """
        raise NotImplementedError
