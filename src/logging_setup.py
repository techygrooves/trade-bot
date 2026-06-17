"""Logging configuration: console + rotating file handler."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs"
_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure the root logger once and return the bot logger."""
    LOG_DIR.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())

    # Avoid duplicate handlers if called more than once.
    if root.handlers:
        return logging.getLogger("bot")

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    return logging.getLogger("bot")
