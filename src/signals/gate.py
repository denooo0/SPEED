"""Cheap rules-based screen — decides when to invoke the LLM brain.

The brain costs money. The gate burns no tokens. Only when the gate fires does
the brain see this cycle. Faithful to mandate Law 1 (default is NO): a screen
that says NO 95% of the time IS the discipline.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class GateConfig:
    invoke_on_volume_spike: bool = True
    invoke_on_bos: bool = True
    invoke_on_session_open: bool = True
    cooldown_seconds: int = 300  # don't fire the gate twice within this window


class CycleGate:
    """Decide whether the current cycle warrants an LLM call."""

    def __init__(self, config: GateConfig) -> None:
        self.config = config
        self._last_fired_at: float = 0.0
        self._last_session: Optional[str] = None

    def should_invoke(self, feature_pack: Dict[str, Any]) -> tuple[bool, str]:
        """Return (decision, reason)."""
        now = time.time()
        if (now - self._last_fired_at) < self.config.cooldown_seconds:
            return (False, "cooldown")

        triggers: list[str] = []
        flow_m5 = feature_pack.get("lens_flow", {}).get("m5", {})
        if self.config.invoke_on_volume_spike and flow_m5.get("volume_spike"):
            triggers.append("volume_spike")

        struct_m5 = feature_pack.get("lens_structure", {}).get("m5", {})
        if self.config.invoke_on_bos and struct_m5.get("last_bos") in ("bull", "bear"):
            triggers.append(f"bos_{struct_m5['last_bos']}")

        ctx = feature_pack.get("lens_context", {})
        session = ctx.get("session")
        if (
            self.config.invoke_on_session_open
            and session in ("london", "ny", "ny-overlap")
            and session != self._last_session
        ):
            triggers.append(f"session_open_{session}")

        # Track session transitions regardless of whether we fired
        if session is not None:
            self._last_session = session

        if triggers:
            self._last_fired_at = now
            reason = ",".join(triggers)
            logger.info("gate fired: %s", reason)
            return (True, reason)
        return (False, "no_trigger")
