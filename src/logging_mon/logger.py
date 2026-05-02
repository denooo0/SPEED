"""Centralised logging setup with rotating file handlers."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict


def setup_logging(cfg: Dict[str, Any]) -> None:
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    log_dir = Path(cfg.get("file_path", "./logs/"))
    log_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = int(cfg.get("max_file_size_mb", 50)) * 1024 * 1024
    backups = int(cfg.get("backup_count", 5))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    for name in ("app", "signals", "trades", "errors"):
        handler = RotatingFileHandler(
            log_dir / f"{name}.log",
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        if name == "errors":
            handler.setLevel(logging.ERROR)
        root.addHandler(handler)
