"""Access-token storage.  The token is kept in QgsSettings (the QGIS profile store), never in a project
file or a log.  The store comes from ``PluginSettings``, so another QSettings (a temporary file, say) can
stand in for it."""
from __future__ import annotations

from typing import Optional

TOKEN_KEY = "cloud/token"


class TokenStore:
    def __init__(self, settings):
        self._settings = settings

    def _s(self):
        return self._settings._s, self._settings.PREFIX

    def get(self) -> str:
        s, p = self._s()
        return str(s.value(p + TOKEN_KEY, "") or "")

    def set(self, token: str) -> None:
        s, p = self._s()
        s.setValue(p + TOKEN_KEY, token or "")

    def clear(self) -> None:
        self.set("")
