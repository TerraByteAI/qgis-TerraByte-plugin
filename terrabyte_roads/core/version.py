"""Plugin version, read from metadata.txt (single source of truth)."""
from __future__ import annotations

import configparser
from functools import lru_cache

from .paths import PLUGIN_DIR


@lru_cache(maxsize=1)
def plugin_version() -> str:
    cp = configparser.ConfigParser()
    try:
        cp.read(PLUGIN_DIR / "metadata.txt", encoding="utf-8")
        return cp.get("general", "version", fallback="0.0.0")
    except configparser.Error:
        return "0.0.0"
