"""Filesystem store for forged hypotheses.

Live hypotheses are YAML at `research/hypotheses/<id>.yml` (the shape
`BacktestRunner.evaluate_hypothesis` loads). Failed ones are retired to
`research/retired/<id>.md` with reason codes — re-test allowed after 90 days or
when a new feature is added (`plan/edge_generation_workflow.md`).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List

import yaml

from src.research.hypothesis import Hypothesis


class HypothesisStore:
    def __init__(self, root: str | Path = "research/") -> None:
        self.root = Path(root)
        self.hypotheses_dir = self.root / "hypotheses"
        self.retired_dir = self.root / "retired"
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.hypotheses_dir.mkdir(parents=True, exist_ok=True)
        self.retired_dir.mkdir(parents=True, exist_ok=True)

    def save(self, hypothesis: Hypothesis) -> Path:
        """Persist a live hypothesis as YAML the backtest runner can load."""
        path = self.hypotheses_dir / f"{hypothesis.id}.yml"
        path.write_text(
            yaml.safe_dump(hypothesis.to_runner_dict(), sort_keys=False, allow_unicode=True)
        )
        return path

    def load(self, hypothesis_id: str) -> Hypothesis:
        path = self.hypotheses_dir / f"{hypothesis_id}.yml"
        return Hypothesis.model_validate(yaml.safe_load(path.read_text()))

    def list_live(self) -> List[Path]:
        return sorted(self.hypotheses_dir.glob("*.yml"))

    def retire(self, hypothesis: Hypothesis, reasons: List[str]) -> Path:
        """Move a failed hypothesis to retired/ with reason codes.

        Removes the live YAML if present so the runner won't re-pick it.
        """
        live = self.hypotheses_dir / f"{hypothesis.id}.yml"
        if live.exists():
            live.unlink()
        reason_block = "\n".join(f"- {r}" for r in reasons) or "- (no reason given)"
        retest_after = time.strftime(
            "%Y-%m-%d", time.gmtime(time.time() + 90 * 86400)
        )
        body = (
            f"# RETIRED — {hypothesis.id}\n\n"
            f"**Thesis:** {hypothesis.thesis}\n\n"
            f"**Mechanism:** {hypothesis.mechanism}\n\n"
            f"## Fail reasons\n{reason_block}\n\n"
            f"## Re-test policy\n"
            f"Re-test allowed after {retest_after} (90 days) OR when a new feature "
            f"is added that changes the inputs.\n"
        )
        path = self.retired_dir / f"{hypothesis.id}.md"
        path.write_text(body)
        return path
