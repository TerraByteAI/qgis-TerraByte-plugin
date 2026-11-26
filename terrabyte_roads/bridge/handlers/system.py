"""System tools: ping."""
from __future__ import annotations

import platform
import sys
from typing import Any, Dict

from qgis.core import Qgis

from ...core.version import plugin_version
from .. import registry
from ..registry import command


@command("ping")
def ping(ctx) -> Dict[str, Any]:
    return {
        "pong": True,
        "qgis_version": Qgis.version(),
        "plugin_version": plugin_version(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "commands": registry.command_names(),
    }
