"""Human-approval deployment for meta-loop changes.

Every ProposedChange that clears the oracle + critic gates is presented
to the operator. Operator approves / rejects / modifies. Approved
changes deploy as a single git commit so rollback is `git revert <sha>`.

NEVER auto-deploys. The approval gate is non-optional.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.meta.critic import CritiqueResult
from src.meta.oracle import OracleResult
from src.meta.proposer import ProposedChange


@dataclass
class DeploymentPackage:
    """Everything the operator sees before approving a change."""
    change: ProposedChange
    oracle: OracleResult
    critique: CritiqueResult
    diff_preview: str            # the literal git diff that would land
    rollback_command: str        # the one-liner that undoes this


class HumanApprovalDeployer:
    """Presents the package, captures the decision, performs the commit.

    Refuses to deploy if:
      - critique.verdict == "blocking_objection"
      - oracle.passed == False
      - operator does not explicitly approve
      - diff is empty or doesn't apply cleanly
    """

    def present(self, package: DeploymentPackage) -> str:
        """Render the package as a markdown brief for the operator."""
        raise NotImplementedError

    def deploy(self, package: DeploymentPackage, operator_decision: str) -> Optional[str]:
        """If decision == 'approve', commit and return the SHA. Else return None."""
        raise NotImplementedError
