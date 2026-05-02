"""Shared test fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def base_config():
    return {
        "TRADING": {"symbol": "XAUUSD", "account_risk_pct": 2.0},
        "INDICATORS": {
            "ma_fast": 5,
            "ma_slow": 10,
            "ad_extreme_threshold": -1_000,
            "volume_spike_multiplier": 2.0,
            "volume_lookback": 5,
        },
        "ENTRY_RULES": {
            "min_spike_pips": 80,
            "min_spike_candles": 5,
            "consolidation_min_candles": 5,
            "consolidation_max_range_pips": 20,
        },
        "POSITION_SIZING": {
            "max_units_per_trade": 12,
            "pyramid_entries": 3,
            "pyramid_size_each": 4,
        },
        "EXIT_RULES": {
            "scale_out_pct": [25, 25, 25, 25],
            "tp_distances": [15, 30, 60, 120],
            "sl_distance": 25,
            "trail_after_pips": 30,
        },
        "TELEGRAM": {"token": "", "chat_id": "", "max_messages_per_min": 5, "enabled": False},
        "BYBIT": {"testnet": True, "api_key": "", "api_secret": ""},
        "LOGGING": {"level": "WARNING", "file_path": "./logs/", "max_file_size_mb": 1, "backup_count": 1},
        "DATABASE": {"path": "./database/"},
        "MONITOR": {"cycle_interval_seconds": 60, "data_stale_threshold_seconds": 300},
        "SIGNAL": {"min_signal_interval_seconds": 0, "max_price_drift_pips": 1000},
    }
