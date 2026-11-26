"""Is the installed copy of the plugin complete and of one version?

An in-place upgrade over an older build can fail half way on Windows (files held open by the old build), leaving a
mix of new and old files.  The ZIP builder writes ``MANIFEST.json`` (file name -> sha256) into the package; at load the
files are compared with it.  A source checkout has no manifest and is not checked.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List

MANIFEST = "MANIFEST.json"
MESSAGE = ("Road Extractor did not update completely. 1. In Plugins > Installed, untick TerraByte Road Extractor. "
           "2. Restart QGIS. 3. Plugins > Install from ZIP, choose the ZIP. 4. Tick TerraByte Road Extractor again.")


BAR_MESSAGE = "Road Extractor did not update completely. Open its panel for the steps."


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def problems(pkg_dir: Path) -> List[str]:
    """Names of files that are missing or differ from the manifest; empty when complete (or when there is no manifest)."""
    pkg_dir = Path(pkg_dir)
    mf = pkg_dir / MANIFEST
    if not mf.is_file():
        return []
    try:
        files = json.loads(mf.read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return [MANIFEST]
    bad = []
    for name, digest in sorted(files.items()):
        p = pkg_dir / name
        try:
            if not p.is_file() or sha256_of(p) != digest:
                bad.append(name)
        except OSError:
            bad.append(name)
    return bad


class IncompletePlugin:
    """Stands in for the plugin when files are missing: standard library and ``qgis`` only, so a copy with any file
    missing still shows its card instead of QGIS's own "Couldn't load plugin" dialog."""

    def __init__(self, iface, bad):
        self.iface, self.bad, self.dock = iface, list(bad), None

    def initGui(self):
        try:
            from qgis.core import Qgis, QgsMessageLog
            from qgis.PyQt.QtCore import Qt
            from qgis.PyQt.QtWidgets import QDockWidget, QLabel, QVBoxLayout, QWidget

            QgsMessageLog.logMessage(f"incomplete install, {len(self.bad)} files missing or different: "
                                     f"{', '.join(self.bad[:8])}", "Road Extractor", Qgis.MessageLevel.Critical)
            self.iface.messageBar().pushMessage("", BAR_MESSAGE, Qgis.MessageLevel.Critical, 0)
            dock = QDockWidget("Road Extractor", self.iface.mainWindow())
            dock.setObjectName("TerraByteRoadExtractorDock")
            body = QWidget()
            lay = QVBoxLayout(body)
            lb = QLabel(MESSAGE)
            lb.setWordWrap(True)
            lay.addWidget(lb)
            lay.addStretch(1)
            dock.setWidget(body)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            self.dock = dock
        except Exception:
            self.dock = None

    def unload(self):
        try:
            if self.dock is not None:
                self.iface.removeDockWidget(self.dock)
                self.dock.deleteLater()
        except Exception:
            pass
        self.dock = None
