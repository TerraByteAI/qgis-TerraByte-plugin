"""Qt helpers for QGIS 3.44 (Qt5) and QGIS 4.2+ (Qt6).

Rules for all plugin code: import Qt only via ``qgis.PyQt``; fully scoped enums
(``Qt.AlignmentFlag.AlignLeft``, ``Qgis.MessageLevel.Info``); ``exec()`` not ``exec_()``;
``QAction``/``QShortcut`` live in ``QtGui``.

QGIS 3.44's ``qgis.PyQt`` compatibility layer exposes the Qt6-style action imports
and scoped enums on Qt5 too. Keep those shared APIs instead of importing PyQt
directly or adding separate Qt5 UI implementations. ``QgsField`` accepts
``QMetaType.Type`` on both supported QGIS versions.
"""
from __future__ import annotations

from qgis.PyQt.QtGui import QAction, QActionGroup
