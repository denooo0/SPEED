"""Load and validate config.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

REQUIRED_SECTIONS = (
    "TRADING",
    "INDICATORS",
    "ENTRY_RULES",
    "EXIT_RULES",
    "POSITION_SIZING",
    "TELEGRAM",
    "BYBIT",
    "LOGGING",
    "DATABASE",
    "MONITOR",
    "SIGNAL",
)


class ConfigError(ValueError):
    pass


def load_config(path: str | Path = "config.yaml") -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    cfg = yaml.safe_load(p.read_text())
    if not isinstance(cfg, dict):
        raise ConfigError("Config root must be a mapping")
    _validate(cfg)
    return cfg


def _validate(cfg: Dict[str, Any]) -> None:
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg]
    if missing:
        raise ConfigError(f"Missing required config sections: {missing}")

    risk_pct = cfg["TRADING"].get("account_risk_pct")
    if risk_pct is None or not (0 < risk_pct <= 10):
        raise ConfigError("TRADING.account_risk_pct must be between 0 and 10")

    tp_distances = cfg["EXIT_RULES"].get("tp_distances")
    if not tp_distances or len(tp_distances) < 3:
        raise ConfigError("EXIT_RULES.tp_distances needs at least 3 values")
