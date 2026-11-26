"""Filesystem locations. Everything user-writable lives under the active QGIS profile:

    <profile>/terrabyte_roads/
        logs/

No hard-coded OS paths: always derive from QgsApplication.qgisSettingsDirPath() so it
works for the official Linux packages, Flatpak, Windows (OSGeo4W/standalone) and macOS.
The TERRABYTE_ROADS_DATA_DIR environment variable, when set, redirects everything (useful for testing).
"""
from __future__ import annotations

import os
from pathlib import Path

PLUGIN_ID = "terrabyte_roads"
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    override = os.environ.get("TERRABYTE_ROADS_DATA_DIR")
    if override:
        base = Path(override)
    else:
        from qgis.core import QgsApplication

        base = Path(QgsApplication.qgisSettingsDirPath()) / PLUGIN_ID
    base.mkdir(parents=True, exist_ok=True)
    return base


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def remove_old_models() -> bool:
    """Delete ``<profile>/terrabyte_roads/models``, a folder of downloaded files that older versions of the plugin
    left and nothing reads now, silently, whenever it is found.  True when one was removed."""
    import shutil

    try:
        p = data_dir() / "models"
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            return not p.exists()
    except Exception:
        pass
    return False
