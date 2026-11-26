"""Typed access to plugin settings stored in QgsSettings under ``terrabyte_roads/``."""
from __future__ import annotations


class PluginSettings:
    PREFIX = "terrabyte_roads/"

    def __init__(self, qsettings=None):
        if qsettings is None:
            from qgis.core import QgsSettings

            qsettings = QgsSettings()
        self._s = qsettings

    def _get(self, key, default, typ):
        v = self._s.value(self.PREFIX + key, default)
        if typ is bool:
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            return bool(v)
        try:
            return typ(v)
        except (TypeError, ValueError):
            return default

    def _set(self, key, value):
        self._s.setValue(self.PREFIX + key, value)

    @property
    def panel_at_unload(self) -> str:
        """``"<process id>:1"`` when the plugin was unloaded with the panel open (``":0"`` closed), for the copy
        that loads next in the same QGIS session (Install from ZIP over a running copy, a reload, untick and tick)."""
        return self._get("ui/panel_at_unload", "", str)

    @panel_at_unload.setter
    def panel_at_unload(self, v: str) -> None:
        self._set("ui/panel_at_unload", str(v))

    @property
    def output_dir(self) -> str:
        return self._get("output/dir", "", str)

    @output_dir.setter
    def output_dir(self, v: str) -> None:
        self._set("output/dir", str(v))
