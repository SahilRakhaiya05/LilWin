"""Structured logging for lil-agents. Call ``setup_logging()`` once at app start."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

_LOG_DIR_ENV = "LIL_AGENTS_LOG_DIR"
_DEFAULT_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5

_configured = False


def app_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.expanduser("~")
    return os.path.join(base, "lil-agents")


def log_dir() -> str:
    override = os.environ.get(_LOG_DIR_ENV)
    if override:
        return override
    return os.path.join(app_data_dir(), "logs")


def setup_logging(level: int = logging.INFO, log_file: Optional[str] = None) -> str:
    """Configure root logger. Idempotent. Returns the active log file path."""
    global _configured
    if _configured:
        return log_file or ""

    target_dir = os.path.dirname(log_file) if log_file else log_dir()
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError:
        target_dir = os.getcwd()

    path = log_file or os.path.join(target_dir, "app.log")

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_DEFAULT_FORMAT)

    try:
        file_h = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
        file_h.setLevel(level)
        file_h.setFormatter(formatter)
        root.addHandler(file_h)
    except OSError:
        pass

    stream_h = logging.StreamHandler(stream=sys.stderr)
    stream_h.setLevel(level)
    stream_h.setFormatter(formatter)
    root.addHandler(stream_h)

    logging.getLogger("websocket").setLevel(logging.WARNING)
    _configured = True
    return path
