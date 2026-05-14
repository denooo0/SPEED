"""Immutable audit log for meta-loop activity.

Every proposer call, oracle result, critic verdict, and operator
decision lands in `meta/audit/<iso-timestamp>-<change-id>.json`. The
audit log is append-only and the loop never modifies past entries.

This is the after-the-fact accountability layer. If a change later
proves bad, the audit log says: who proposed it, with what evidence,
what backtest passed, what the critic missed, who approved.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.meta.critic import CritiqueResult
from src.meta.oracle import OracleResult
from src.meta.proposer import ProposedChange


class AuditLog:
    """Append-only JSON-per-event audit trail under meta/audit/."""

    def __init__(self, root: str | Path = "meta/audit/") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        change: ProposedChange,
        oracle: Optional[OracleResult] = None,
        critique: Optional[CritiqueResult] = None,
        operator_decision: Optional[str] = None,
        deployed_sha: Optional[str] = None,
    ) -> Path:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.root / f"{ts}-{change.change_id}.json"
        payload: Dict[str, Any] = {
            "audit_timestamp": ts,
            "change": asdict(change),
            "oracle": asdict(oracle) if oracle else None,
            "critique": asdict(critique) if critique else None,
            "operator_decision": operator_decision,
            "deployed_sha": deployed_sha,
        }
        path.write_text(json.dumps(payload, indent=2, default=str))
        return path
