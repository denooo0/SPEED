"""Load and validate config.yaml."""
from __future__ import annotations

import os
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

OPTIONAL_SECTIONS_WITH_DEFAULTS = {
    "ANTHROPIC": {
        "api_key": "",
        "model": "claude-opus-4-7",
        "effort": "high",
        "max_tokens": 16000,
        "mandate_path": "prompts/ATLAS_MANDATE.md",
        "enabled": False,
    },
    "MEMORY": {
        "root": "memory/",
        "digest_window": 30,
    },
    "GATE": {
        "invoke_on_volume_spike": True,
        "invoke_on_bos": True,
        "invoke_on_session_open": True,
    },
}


class ConfigError(ValueError):
    pass


def load_config(path: str | Path = "config.yaml") -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    cfg = yaml.safe_load(p.read_text())
    if not isinstance(cfg, dict):
        raise ConfigError("Config root must be a mapping")
    _apply_defaults(cfg)
    _apply_env_overrides(cfg)
    _validate(cfg)
    return cfg


def _apply_defaults(cfg: Dict[str, Any]) -> None:
    for section, defaults in OPTIONAL_SECTIONS_WITH_DEFAULTS.items():
        if section not in cfg:
            cfg[section] = dict(defaults)
        else:
            for k, v in defaults.items():
                cfg[section].setdefault(k, v)


def _apply_env_overrides(cfg: Dict[str, Any]) -> None:
    """Pull secrets from env when config has placeholders or empty values."""
    env_key = os.getenv("ANTHROPIC_API_KEY")
    if env_key and not cfg["ANTHROPIC"].get("api_key"):
        cfg["ANTHROPIC"]["api_key"] = env_key

    bybit_key = os.getenv("BYBIT_API_KEY")
    bybit_secret = os.getenv("BYBIT_API_SECRET")
    if bybit_key and cfg["BYBIT"].get("api_key") in (None, "", "YOUR_API_KEY"):
        cfg["BYBIT"]["api_key"] = bybit_key
    if bybit_secret and cfg["BYBIT"].get("api_secret") in (None, "", "YOUR_API_SECRET"):
        cfg["BYBIT"]["api_secret"] = bybit_secret

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and cfg["TELEGRAM"].get("token") in (None, "", "YOUR_BOT_TOKEN"):
        cfg["TELEGRAM"]["token"] = tg_token
    if tg_chat and cfg["TELEGRAM"].get("chat_id") in (None, "", "YOUR_CHAT_ID"):
        cfg["TELEGRAM"]["chat_id"] = tg_chat


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

    if cfg["ANTHROPIC"].get("enabled") and not cfg["ANTHROPIC"].get("api_key"):
        raise ConfigError(
            "ANTHROPIC.enabled is true but no api_key set "
            "(provide config or ANTHROPIC_API_KEY env var)"
        )
