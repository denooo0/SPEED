"""Configuration: one file, environment-overridable, secrets never journaled.

Precedence: explicit argument > environment variable > config file > default.
Secrets (API keys, bot tokens) are read from the environment ONLY and are never
written to disk by this module, never included in `redacted()`, and never reach
a journal -- journals are permanent and human-readable, so a secret that lands
in one is a secret you have to rotate.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SECRET_KEYS = {"anthropic_api_key", "openai_api_key", "telegram_token",
               "broker_token", "broker_secret"}


@dataclass
class Config:
    # --- storage
    root: str = "./colosseum_data"
    tick_archive: str = "./colosseum_data/ticks"
    durable_ledger: bool = True

    # --- market
    symbol: str = "XAU_USD"
    intervals: Tuple[int, ...] = (1, 15, 60, 300, 900, 3600)
    signal_timeframe_s: int = 300

    # --- cadence
    frame_every_s: int = 1
    health_every_s: int = 30
    checkpoint_every_s: int = 300
    housekeeping_every_s: int = 60

    # --- cost model (XAUUSD, tune to YOUR broker -- defaults are illustrative)
    spread: float = 0.30
    commission_per_unit: float = 0.05
    slippage: float = 0.10
    min_edge_multiple: float = 2.5

    # --- risk envelope
    max_concurrent: int = 4
    max_same_direction: int = 3
    max_drawdown_r: float = 20.0
    target_signals_per_day: float = 5.0

    # --- llm
    llm_provider: str = "anthropic"        # anthropic | openai | none
    llm_model: str = "claude-sonnet-4-5"
    llm_deadline_ms: int = 3000
    llm_max_retries: int = 1
    llm_cost_per_call_usd: float = 0.006
    daily_cost_budget_usd: float = 25.0

    # --- learning
    exploration_rate: float = 0.07
    challenger_fdr: float = 0.10
    challenger_min_shadow_n: int = 40

    # --- delivery
    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    telegram_send_outcomes: bool = True

    # --- secrets (env only)
    anthropic_api_key: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    broker_token: str = field(default="", repr=False)

    # ---- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[str | Path] = None, **overrides) -> "Config":
        data: Dict[str, Any] = {}
        if path and Path(path).exists():
            data.update(json.loads(Path(path).read_text()))

        valid = {f.name: f for f in fields(cls)}
        for name, f in valid.items():
            env = os.environ.get(f"COLOSSEUM_{name.upper()}")
            if env is None and name in SECRET_KEYS:
                env = os.environ.get(name.upper())      # e.g. ANTHROPIC_API_KEY
            if env is not None:
                data[name] = _coerce(env, f.type)

        data.update({k: v for k, v in overrides.items() if k in valid})
        if "intervals" in data and isinstance(data["intervals"], list):
            data["intervals"] = tuple(data["intervals"])
        return cls(**{k: v for k, v in data.items() if k in valid})

    def save(self, path: str | Path) -> Path:
        """Writes NON-SECRET settings only. Secrets stay in the environment."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        d = {k: (list(v) if isinstance(v, tuple) else v)
             for k, v in asdict(self).items() if k not in SECRET_KEYS}
        p.write_text(json.dumps(d, indent=2, sort_keys=True))
        return p

    def redacted(self) -> Dict[str, Any]:
        """Safe for logs, journals, and incident reports."""
        out: Dict[str, Any] = {}
        for k, v in asdict(self).items():
            if k in SECRET_KEYS:
                out[k] = "***SET***" if v else "***UNSET***"
            elif isinstance(v, tuple):
                out[k] = list(v)
            else:
                out[k] = v
        return out

    def validate(self) -> Tuple[bool, list]:
        """Fail loudly at startup rather than mysteriously at 3am."""
        errs = []
        if self.min_edge_multiple < 1.0:
            errs.append("min_edge_multiple < 1.0 means signals may not clear "
                        "their own cost -- the learner would train on fantasy")
        if self.spread <= 0:
            errs.append("spread must be > 0; a zero-cost model inflates every "
                        "downstream statistic")
        if self.signal_timeframe_s not in self.intervals:
            errs.append(f"signal_timeframe_s {self.signal_timeframe_s} is not in "
                        f"intervals {self.intervals}")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            errs.append("llm_provider=anthropic but ANTHROPIC_API_KEY is unset")
        if self.llm_provider == "openai" and not self.openai_api_key:
            errs.append("llm_provider=openai but OPENAI_API_KEY is unset")
        if self.telegram_enabled and not (self.telegram_token and self.telegram_chat_id):
            errs.append("telegram_enabled but token/chat_id missing")
        if not 0.0 <= self.exploration_rate <= 0.5:
            errs.append("exploration_rate should sit in [0, 0.5]")
        if self.exploration_rate < 0.02:
            errs.append("exploration_rate < 0.02 will plateau the learner at a "
                        "local optimum while looking healthy")
        return (not errs), errs


def _coerce(raw: str, typ: Any) -> Any:
    s = str(typ)
    if "bool" in s:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if "int" in s and "float" not in s:
        return int(float(raw))
    if "float" in s:
        return float(raw)
    if "Tuple" in s or "tuple" in s:
        return tuple(int(x) for x in raw.replace(" ", "").split(",") if x)
    return raw
