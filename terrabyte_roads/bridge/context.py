"""Context passed to every bridge handler, plus shared helpers for handlers.

Handlers must work both inside the QGIS GUI (``iface`` set) and headless
(``iface`` is None). Use ``ctx.get_map_settings()/get_map_extent()/set_map_extent()``
instead of touching ``iface.mapCanvas()`` directly.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapLayer,
    QgsMapSettings,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize

from ..core.settings import PluginSettings
from ..core.tasks import TaskManager
from .registry import CommandError


class BridgeContext:
    def __init__(self, iface: Any = None, tasks: Optional[TaskManager] = None,
                 settings: Optional[PluginSettings] = None, plugin: Any = None):
        self.iface = iface
        self.tasks = tasks or TaskManager()
        self.settings = settings or PluginSettings()
        self.plugin = plugin
        self._extent: Optional[QgsRectangle] = None
        self._extent_crs: Optional[QgsCoordinateReferenceSystem] = None

    @property
    def project(self) -> QgsProject:
        return QgsProject.instance()

    @property
    def canvas(self):
        return self.iface.mapCanvas() if self.iface is not None else None

    def map_crs(self) -> QgsCoordinateReferenceSystem:
        if self.canvas is not None:
            return self.canvas.mapSettings().destinationCrs()
        crs = self.project.crs()
        return crs if crs.isValid() else QgsCoordinateReferenceSystem("EPSG:4326")

    def get_map_extent(self) -> QgsRectangle:
        if self.canvas is not None:
            return self.canvas.extent()
        if self._extent is None or self._extent.isEmpty():
            return self._project_full_extent()
        if self._extent_crs is not None and self._extent_crs != self.map_crs():
            return transform_rect(self._extent, self._extent_crs, self.map_crs())
        return QgsRectangle(self._extent)

    def set_map_extent(self, rect: QgsRectangle, crs: Optional[QgsCoordinateReferenceSystem] = None) -> None:
        crs = crs or self.map_crs()
        target = self.map_crs()
        if crs != target:
            rect = transform_rect(rect, crs, target)
        if self.canvas is not None:
            self.canvas.setExtent(rect)
            self.canvas.refresh()
        else:
            self._extent, self._extent_crs = QgsRectangle(rect), target

    def get_map_settings(self, width: int = 1024, height: int = 768,
                         layers: Optional[Sequence[QgsMapLayer]] = None) -> QgsMapSettings:
        """Map settings mirroring the canvas (GUI) or the visible project layers (headless)."""
        if self.canvas is not None:
            ms = QgsMapSettings(self.canvas.mapSettings())
        else:
            ms = QgsMapSettings()
            ms.setDestinationCrs(self.map_crs())
            root = self.project.layerTreeRoot()
            ms.setLayers([l for l in root.layerOrder()
                          if root.findLayer(l.id()) is not None and root.findLayer(l.id()).isVisible()])
            ms.setExtent(self.get_map_extent())
        if layers is not None:
            ms.setLayers(list(layers))
        ms.setOutputSize(QSize(int(width), int(height)))
        return ms

    def _project_full_extent(self) -> QgsRectangle:
        crs = self.map_crs()
        full = QgsRectangle()
        for layer in self.project.mapLayers().values():
            ext = layer.extent()
            if ext.isEmpty() or not layer.crs().isValid():
                continue
            try:
                ext = transform_rect(ext, layer.crs(), crs)
            except Exception:
                continue
            if full.isEmpty():
                full = QgsRectangle(ext)
            else:
                full.combineExtentWith(ext)
        return full

    def get_layer(self, ref: str, layer_type: Optional[str] = None) -> QgsMapLayer:
        """Find a layer by ID, or by exact (then case-insensitive) unique name.

        layer_type: None | "raster" | "vector" - raise invalid_params on mismatch.
        """
        if not isinstance(ref, str) or not ref:
            raise CommandError("invalid_params", "layer_id must be a non-empty string")
        layer = self.project.mapLayer(ref)
        if layer is None:
            matches = self.project.mapLayersByName(ref)
            if not matches:
                matches = [l for l in self.project.mapLayers().values() if l.name().lower() == ref.lower()]
            if len(matches) > 1:
                raise CommandError("invalid_params",
                                   f"layer name {ref!r} is ambiguous; use one of the IDs: "
                                   + ", ".join(l.id() for l in matches[:10]))
            layer = matches[0] if matches else None
        if layer is None:
            raise CommandError("not_found", f"no layer with id or name {ref!r} (call list_layers)")
        if layer_type is not None and layer_kind(layer) != layer_type:
            raise CommandError("invalid_params", f"layer {layer.name()!r} is {layer_kind(layer)}, expected {layer_type}")
        return layer


def layer_kind(layer: QgsMapLayer) -> str:
    """"vector" | "raster" | "other" (mesh, vector tile, point cloud, ...)."""
    if isinstance(layer, QgsVectorLayer):
        return "vector"
    if isinstance(layer, QgsRasterLayer):
        return "raster"
    return "other"


def parse_crs(value: Any, default: str = "EPSG:4326") -> QgsCoordinateReferenceSystem:
    """Accepts 'EPSG:xxxx', an int EPSG code, WKT or a PROJ string."""
    if value is None or value == "":
        value = default
    if isinstance(value, int):
        value = f"EPSG:{value}"
    crs = QgsCoordinateReferenceSystem(str(value))
    if not crs.isValid():
        raise CommandError("invalid_params", f"invalid CRS {value!r}")
    return crs


def crs_id(crs: QgsCoordinateReferenceSystem) -> str:
    return crs.authid() or crs.toWkt()[:120]


def transform_rect(rect: QgsRectangle, src: QgsCoordinateReferenceSystem,
                   dst: QgsCoordinateReferenceSystem) -> QgsRectangle:
    if src == dst:
        return QgsRectangle(rect)
    tr = QgsCoordinateTransform(src, dst, QgsProject.instance())
    return tr.transformBoundingBox(rect)


def parse_bbox(bbox: Any) -> QgsRectangle:
    """[xmin, ymin, xmax, ymax] -> QgsRectangle (validated)."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise CommandError("invalid_params", "bbox must be [xmin, ymin, xmax, ymax]")
    try:
        xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    except (TypeError, ValueError):
        raise CommandError("invalid_params", "bbox values must be numbers") from None
    if not (xmin < xmax and ymin < ymax):
        raise CommandError("invalid_params", "bbox must satisfy xmin < xmax and ymin < ymax")
    return QgsRectangle(xmin, ymin, xmax, ymax)


def extent_dict(rect: QgsRectangle, crs: QgsCoordinateReferenceSystem, digits: int = 6) -> Dict[str, Any]:
    return {
        "xmin": round(rect.xMinimum(), digits), "ymin": round(rect.yMinimum(), digits),
        "xmax": round(rect.xMaximum(), digits), "ymax": round(rect.yMaximum(), digits),
        "crs": crs_id(crs),
    }


def wgs84_extent(rect: QgsRectangle, crs: QgsCoordinateReferenceSystem) -> Dict[str, Any]:
    return extent_dict(transform_rect(rect, crs, QgsCoordinateReferenceSystem("EPSG:4326")),
                       QgsCoordinateReferenceSystem("EPSG:4326"))


def clamp_limit(limit: Any, default: int, maximum: int) -> int:
    try:
        n = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        raise CommandError("invalid_params", "limit must be an integer") from None
    return max(1, min(n, maximum))


_SECRET_PARAM = re.compile(
    r"(?i)((?:[a-z0-9_.-]*(?:key|token|sig|signature|secret|password|passwd|pwd|credential|auth)[a-z0-9_.-]*)"
    r"(?:=|%3d))[^&\s'\"]*")


def short_source(source: str, n: int = 160) -> str:
    """Trim a layer source for display; credentials in query strings (SAS ``sig=``, ``apikey=``,
    ``access_token=``, ``X-Amz-Signature=`` ...) are replaced by ``***``."""
    s = _SECRET_PARAM.sub(lambda m: m.group(1) + "***", source)
    if "sig=" in source or "token" in source.lower():
        s = s.split("?", 1)[0]
    return s if len(s) <= n else s[: n - 3] + "..."


def require_str(name: str, value: Any, choices: Optional[List[str]] = None) -> str:
    if not isinstance(value, str) or not value:
        raise CommandError("invalid_params", f"{name} must be a non-empty string")
    if choices is not None and value not in choices:
        raise CommandError("invalid_params", f"{name} must be one of {choices}")
    return value
