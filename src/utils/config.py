"""Settings persistence (non-sensitive). Secrets live in ``utils.secrets``."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from utils.secrets import SecretsManager

logger = logging.getLogger(__name__)

_SECRET_KEYS = ("authToken",)


def _migrate_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Pull secret values out of the settings dict and into SecretsManager.

    Runs once per load: if a legacy ``authToken`` appears in ``settings.json``,
    copy it into the secrets file and remove it from the returned dict. The
    in-memory settings stay clean so subsequent saves don't rewrite it.
    """
    moved: Dict[str, Any] = {}
    for key in _SECRET_KEYS:
        if key in data and data.get(key):
            moved[key] = data.pop(key)
        elif key in data:
            data.pop(key)
    if moved:
        mgr = SecretsManager()
        existing = mgr.load()
        for k, v in moved.items():
            if not existing.get(k):
                existing[k] = v
        mgr.save(existing)
        logger.info("Migrated %s into secrets store", ",".join(moved.keys()))
    return data


class ConfigManager:
    def __init__(self, config_path: Optional[str] = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self.config_path = config_path or os.path.join(base_dir, "config", "settings.json")

    def load(self) -> Dict[str, Any]:
        if not os.path.isfile(self.config_path):
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read settings at %s", self.config_path, exc_info=True)
            return {}
        mutated = _migrate_secrets(data)
        if mutated is not data:
            data = mutated
        return data

    def save(self, data: Dict[str, Any]) -> None:
        clean = {k: v for k, v in data.items() if k not in _SECRET_KEYS}
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=4)
        except OSError:
            logger.error("Could not write settings to %s", self.config_path, exc_info=True)
