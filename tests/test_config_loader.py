"""Config loader smoke tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config.config_loader import ConfigError, load_config


def test_load_repo_config():
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg["TRADING"]["symbol"] == "XAUUSD"
    assert cfg["TRADING"]["account_risk_pct"] > 0
    assert len(cfg["EXIT_RULES"]["tp_distances"]) >= 3


def test_missing_file_raises():
    with pytest.raises(ConfigError):
        load_config("/tmp/does_not_exist_xyz.yaml")
