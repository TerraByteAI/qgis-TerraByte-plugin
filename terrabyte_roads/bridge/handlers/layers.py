"""Layer tools: list/inspect/add/remove/style/show/zoom."""
from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsMapLayer,
    QgsRasterLayer,
    QgsRectangle,
    QgsRendererCategory,
    QgsSymbol,
    QgsUnitTypes,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

from ..context import (BridgeContext, clamp_limit, crs_id, extent_dict, layer_kind, parse_crs,
                       require_str, short_source, transform_rect, wgs84_extent)
from ..registry import CommandError, command

STANDARD_ROAD_COLORS: Dict[str, str] = {
    "highway": "#ff7f00",
    "local_road": "#ffd400",
    "apron_taxiway": "#ff69b4",
    "road": "#ffd400",
}
OTHER_COLOR = "#999999"
MAX_CATEGORIES = 100
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_METERS_PER_DEG_LAT = 110574.0
_METERS_PER_DEG_LON_EQ = 111320.0


def _is_visible(ctx: BridgeContext, layer: QgsMapLayer) -> bool:
    node = ctx.project.layerTreeRoot().findLayer(layer.id())
    return bool(node.isVisible()) if node is not None else False


def _safe_extent(layer: QgsMapLayer) -> Optional[Dict[str, Any]]:
    try:
        ext = layer.extent()
        if ext.isNull() or not layer.crs().isValid():
            return None
        return wgs84_extent(ext, layer.crs())
    except Exception:
        return None


def _layer_extent_dict(layer: QgsMapLayer) -> Optional[Dict[str, Any]]:
    ext = layer.extent()
    if ext.isNull():
        return None
    return extent_dict(ext, layer.crs())


def _geom_name(layer: QgsVectorLayer) -> str:
    return layer.geometryType().name


def _provider_name(layer: QgsMapLayer) -> str:
    return layer.providerType() if layer.dataProvider() is not None else ""


def _raster_resolution_m(layer: QgsRasterLayer) -> Optional[float]:
    """Approximate pixel size in metres (geographic CRS: at the extent's centre latitude)."""
    crs = layer.crs()
    px, py = layer.rasterUnitsPerPixelX(), layer.rasterUnitsPerPixelY()
    if not crs.isValid() or px <= 0 or py <= 0:
        return None
    if crs.isGeographic():
        lat = layer.extent().center().y()
        mx = px * _METERS_PER_DEG_LON_EQ * math.cos(math.radians(max(-89.0, min(89.0, lat))))
        my = py * _METERS_PER_DEG_LAT
        return round((mx + my) / 2.0, 4)
    f = QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), Qgis.DistanceUnit.Meters)
    return round((px + py) / 2.0 * f, 4)


def _zoom(ctx: BridgeContext, layer: QgsMapLayer) -> None:
    ext = layer.extent()
    if ext.isNull():
        raise CommandError("unavailable", f"layer {layer.name()!r} has no extent to zoom to")
    rect = QgsRectangle(ext)
    if rect.width() == 0 or rect.height() == 0:
        d = max(rect.width(), rect.height()) or (0.001 if layer.crs().isGeographic() else 100.0)
        rect.grow(d)
    else:
        rect.scale(1.05)
    ctx.set_map_extent(rect, layer.crs())


def _after_add(ctx: BridgeContext, layer: QgsMapLayer, zoom_to: bool) -> None:
    ctx.project.addMapLayer(layer)
    if zoom_to:
        _zoom(ctx, layer)


def _name_from(source: str) -> str:
    s = source.split("?", 1)[0].rstrip("/")
    base = os.path.basename(s) or s
    if "://" not in source and not source.startswith("/vsi"):
        base = os.path.splitext(base)[0] or base
    return base or "layer"


@command("list_layers")
def list_layers(ctx: BridgeContext, type_filter: str = "all", limit: int = 100) -> Dict[str, Any]:
    require_str("type_filter", type_filter, ["all", "raster", "vector"])
    n = clamp_limit(limit, 100, 500)
    root = ctx.project.layerTreeRoot()
    ordered = [l for l in root.layerOrder()]
    seen = {l.id() for l in ordered}
    ordered += [l for l in ctx.project.mapLayers().values() if l.id() not in seen]
    rows: List[Dict[str, Any]] = []
    total = 0
    for layer in ordered:
        kind = layer_kind(layer)
        if type_filter != "all" and kind != type_filter:
            continue
        total += 1
        if len(rows) >= n:
            continue
        row: Dict[str, Any] = {"id": layer.id(), "name": layer.name(), "type": kind,
                               "visible": _is_visible(ctx, layer), "crs": crs_id(layer.crs()),
                               "provider": _provider_name(layer)}
        if isinstance(layer, QgsVectorLayer):
            row["geometry_type"] = _geom_name(layer)
            row["feature_count"] = layer.featureCount()
        elif isinstance(layer, QgsRasterLayer):
            row["band_count"] = layer.bandCount()
        rows.append(row)
    return {"layers": rows, "total": total, "truncated": total > len(rows)}


@command("get_layer_info")
def get_layer_info(ctx: BridgeContext, layer_id: str) -> Dict[str, Any]:
    layer = ctx.get_layer(layer_id)
    kind = layer_kind(layer)
    info: Dict[str, Any] = {
        "id": layer.id(), "name": layer.name(), "type": kind, "provider": _provider_name(layer),
        "source": short_source(layer.source()), "crs": crs_id(layer.crs()),
        "extent": _layer_extent_dict(layer), "extent_wgs84": _safe_extent(layer),
        "visible": _is_visible(ctx, layer),
    }
    if isinstance(layer, QgsVectorLayer):
        fields = layer.fields()
        info.update(
            geometry_type=_geom_name(layer), feature_count=layer.featureCount(),
            fields=[{"name": f.name(), "type": f.typeName()} for f in list(fields)[:100]],
            selected_count=layer.selectedFeatureCount(),
        )
    elif isinstance(layer, QgsRasterLayer):
        prov = layer.dataProvider()
        bands = []
        for i in range(1, min(layer.bandCount(), 20) + 1):
            nodata = prov.sourceNoDataValue(i) if prov.sourceHasNoDataValue(i) else None
            bands.append({"index": i, "name": prov.generateBandName(i),
                          "data_type": prov.dataType(i).name, "nodata": nodata})
        units = layer.crs().mapUnits() if layer.crs().isValid() else Qgis.DistanceUnit.Unknown
        info.update(
            width=layer.width(), height=layer.height(), band_count=layer.bandCount(), bands=bands,
            pixel_size={"x": layer.rasterUnitsPerPixelX(), "y": layer.rasterUnitsPerPixelY(),
                        "units": QgsUnitTypes.toString(units)},
            resolution_m=_raster_resolution_m(layer),
        )
    return info


def _detect_source_type(source: str) -> str:
    low = source.lower()
    if low.startswith("type=xyz") or "{z}" in low or "%7bz%7d" in low:
        return "xyz"
    if low.startswith("/vsicurl/") or low.startswith("/vsis3/") or low.startswith("/vsigs/"):
        return "cog"
    if low.startswith(("http://", "https://")):
        if "service=wms" in low or "request=getmap" in low or "request=getcapabilities" in low or "/wms" in low:
            return "wms"
        return "cog"
    if "url=" in low and "layers=" in low:
        return "wms"
    return "file"


@command("add_raster_layer")
def add_raster_layer(ctx: BridgeContext, source: str, name: Optional[str] = None, source_type: str = "auto",
                     wms_layers: Optional[str] = None, wms_format: str = "image/png",
                     wms_crs: Optional[str] = None, zoom_to: bool = False) -> Dict[str, Any]:
    require_str("source", source)
    require_str("source_type", source_type, ["auto", "file", "url", "cog", "wms", "xyz"])
    source = source.strip()
    stype = _detect_source_type(source) if source_type == "auto" else source_type
    provider = "gdal"
    if stype == "file":
        p = Path(source).expanduser()
        if not p.exists():
            raise CommandError("not_found", f"file not found: {source}")
        uri = str(p)
    elif stype in ("url", "cog"):
        if not source.lower().startswith(("http://", "https://", "/vsicurl/", "/vsis3/", "/vsigs/")):
            raise CommandError("invalid_params", "url/cog sources must start with http:// or https://")
        uri = source if source.startswith("/vsi") else "/vsicurl/" + source
    elif stype == "xyz":
        provider = "wms"
        if source.lower().startswith("type=xyz"):
            uri = source
        else:
            if not source.lower().startswith(("http://", "https://", "file://")):
                raise CommandError("invalid_params", "xyz source must be a tile URL template like "
                                   "https://host/{z}/{x}/{y}.png")
            uri = f"type=xyz&url={quote(source, safe=':/')}&zmin=0&zmax=19"
    else:
        provider = "wms"
        if "url=" in source and "layers=" in source:
            uri = source
        else:
            if not source.lower().startswith(("http://", "https://")):
                raise CommandError("invalid_params", "wms source must be the service URL (http/https)")
            if not wms_layers:
                raise CommandError("invalid_params", "wms_layers is required for WMS sources "
                                   "(comma-separated layer names from GetCapabilities)")
            names = [s.strip() for s in str(wms_layers).split(",") if s.strip()]
            crs = parse_crs(wms_crs, "EPSG:3857")
            parts = [f"crs={crs.authid() or 'EPSG:3857'}", f"format={wms_format}"]
            parts += [f"layers={quote(n, safe='')}" for n in names]
            parts += ["styles" for _ in names]
            parts.append(f"url={quote(source, safe=':/')}")
            uri = "&".join(parts)
    layer = QgsRasterLayer(uri, name or _name_from(source if stype != "wms" else (wms_layers or source)), provider)
    if not layer.isValid():
        why = layer.error().summary() if layer.error().messageList() else "unreadable or unsupported source"
        code = "unavailable" if stype in ("url", "cog", "wms", "xyz") else "invalid_params"
        raise CommandError(code, f"could not load raster {short_source(source)!r} as {stype}: {why}")
    _after_add(ctx, layer, zoom_to)
    return {"layer_id": layer.id(), "name": layer.name(), "type": "raster", "provider": _provider_name(layer),
            "crs": crs_id(layer.crs()), "band_count": layer.bandCount(), "extent_wgs84": _safe_extent(layer)}


@command("add_vector_layer")
def add_vector_layer(ctx: BridgeContext, source: str, name: Optional[str] = None, provider: str = "ogr",
                     zoom_to: bool = False) -> Dict[str, Any]:
    require_str("source", source)
    require_str("provider", provider)
    source = source.strip()
    if provider == "ogr":
        path = source.split("|", 1)[0]
        if not re.match(r"^(/vsi|[a-zA-Z][a-zA-Z0-9+.-]*:)", path) or re.match(r"^[a-zA-Z]:[\\/]", path):
            if not Path(path).expanduser().exists():
                raise CommandError("not_found", f"file not found: {path}")
    layer = QgsVectorLayer(source, name or _name_from(source.split("|", 1)[0]), provider)
    if not layer.isValid():
        raise CommandError("invalid_params", f"could not load vector layer {short_source(source)!r} "
                           f"with provider {provider!r}")
    _after_add(ctx, layer, zoom_to)
    return {"layer_id": layer.id(), "name": layer.name(), "geometry_type": _geom_name(layer),
            "feature_count": layer.featureCount(), "crs": crs_id(layer.crs()), "extent_wgs84": _safe_extent(layer)}


@command("remove_layer")
def remove_layer(ctx: BridgeContext, layer_id: str) -> Dict[str, Any]:
    layer = ctx.get_layer(layer_id)
    lid, name = layer.id(), layer.name()
    ctx.project.removeMapLayer(lid)
    return {"removed": lid, "name": name}


def _road_palette(layer: QgsVectorLayer) -> List[Tuple[str, str, str]]:
    """[(value, label, color)] the standard palette."""
    return [(k, k.replace("_", " ").capitalize(), v) for k, v in STANDARD_ROAD_COLORS.items()]


def _make_symbol(layer: QgsVectorLayer, color: str, width: Optional[float]) -> QgsSymbol:
    sym = QgsSymbol.defaultSymbol(layer.geometryType())
    sym.setColor(QColor(color))
    _apply_width(sym, width if width is not None else (0.6 if layer.geometryType() == Qgis.GeometryType.Line else None))
    return sym


def _apply_width(sym: QgsSymbol, width: Optional[float]) -> None:
    if width is None:
        return
    if sym.type() == Qgis.SymbolType.Line:
        sym.setWidth(width)
    elif sym.type() == Qgis.SymbolType.Marker:
        sym.setSize(width)
    else:
        for i in range(sym.symbolLayerCount()):
            sl = sym.symbolLayer(i)
            if hasattr(sl, "setStrokeWidth"):
                sl.setStrokeWidth(width)


def _auto_color(i: int, n: int) -> str:
    return QColor.fromHsvF((i / max(n, 1)) % 1.0, 0.75, 0.9).name()


def _strip_python_hooks(layer: QgsMapLayer) -> None:
    """A QML file can carry layer actions and form init code that run Python when the user clicks /
    opens a form: drop them always."""
    if not isinstance(layer, QgsVectorLayer):
        return
    layer.actions().clearActions()
    cfg = layer.editFormConfig()
    cfg.setInitCodeSource(Qgis.AttributeFormPythonInitCodeSource.NoSource)
    cfg.setInitCode("")
    cfg.setInitFunction("")
    cfg.setInitFilePath("")
    layer.setEditFormConfig(cfg)


@command("set_layer_style")
def set_layer_style(ctx: BridgeContext, layer_id: str, preset: Optional[str] = None, color: Optional[str] = None,
                    width: Optional[float] = None, opacity: Optional[float] = None,
                    categorize_by: Optional[str] = None, qml_path: Optional[str] = None) -> Dict[str, Any]:
    layer = ctx.get_layer(layer_id)
    if preset is not None and preset != "road_classes":
        raise CommandError("invalid_params", "preset must be 'road_classes'")
    if color is not None and not (isinstance(color, str) and _HEX.match(color)):
        raise CommandError("invalid_params", "color must be '#rrggbb'")
    if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= opacity <= 1):
        raise CommandError("invalid_params", "opacity must be a number between 0 and 1")
    if width is not None and not (isinstance(width, (int, float)) and width > 0):
        raise CommandError("invalid_params", "width must be a positive number (mm)")
    if preset and categorize_by:
        raise CommandError("invalid_params", "use either preset or categorize_by, not both")
    if color is not None and (preset or categorize_by):
        raise CommandError("invalid_params", "color cannot be combined with preset/categorize_by")
    is_vector = isinstance(layer, QgsVectorLayer)
    if (preset or categorize_by or color is not None or width is not None) and not is_vector:
        raise CommandError("invalid_params", "preset/color/width/categorize_by apply to vector layers only "
                           "(rasters: opacity or qml_path)")
    result: Dict[str, Any] = {"layer_id": layer.id()}
    if qml_path:
        p = Path(qml_path).expanduser()
        if not p.is_file():
            raise CommandError("not_found", f"QML file not found: {qml_path}")
        msg, ok = layer.loadNamedStyle(str(p))
        if not ok:
            raise CommandError("invalid_params", f"could not load QML style: {msg}")
        _strip_python_hooks(layer)
    categories: Optional[List[Dict[str, str]]] = None
    if is_vector and (preset or categorize_by):
        field = "class" if preset else categorize_by
        idx = layer.fields().indexOf(field)
        if idx < 0:
            raise CommandError("invalid_params", f"layer {layer.name()!r} has no field {field!r}; fields: "
                               + ", ".join(f.name() for f in list(layer.fields())[:30]))
        if preset:
            pal = _road_palette(layer)
        else:
            values = sorted((v for v in layer.uniqueValues(idx, MAX_CATEGORIES + 1) if v is not None
                             and str(v) != "NULL"), key=str)
            if len(values) > MAX_CATEGORIES:
                raise CommandError("invalid_params", f"field {field!r} has more than {MAX_CATEGORIES} distinct values")
            pal = [(str(v), str(v), _auto_color(i, len(values))) for i, v in enumerate(values)]
        cats = [QgsRendererCategory(v, _make_symbol(layer, c, width), lbl) for v, lbl, c in pal]
        cats.append(QgsRendererCategory("", _make_symbol(layer, OTHER_COLOR, width), "other"))
        layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))
        categories = [{"value": v, "label": lbl, "color": c} for v, lbl, c in pal]
        categories.append({"value": "", "label": "other", "color": OTHER_COLOR})
    elif is_vector and (color is not None or width is not None):
        renderer = layer.renderer()
        sym = renderer.symbol() if renderer is not None and renderer.type() == "singleSymbol" else None
        if sym is None:
            sym = QgsSymbol.defaultSymbol(layer.geometryType())
            from qgis.core import QgsSingleSymbolRenderer

            layer.setRenderer(QgsSingleSymbolRenderer(sym))
        else:
            sym = sym.clone()
            layer.renderer().setSymbol(sym)
            sym = layer.renderer().symbol()
        if color is not None:
            sym.setColor(QColor(color))
        _apply_width(sym, width)
    if opacity is not None:
        layer.setOpacity(float(opacity))
    layer.triggerRepaint()
    renderer = getattr(layer, "renderer", lambda: None)()
    result["renderer"] = renderer.type() if renderer is not None else ("qml" if qml_path else "none")
    if categories is not None:
        result["categories"] = categories
    return result


@command("set_layer_visibility")
def set_layer_visibility(ctx: BridgeContext, layer_id: str, visible: bool) -> Dict[str, Any]:
    if not isinstance(visible, bool):
        raise CommandError("invalid_params", "visible must be true or false")
    layer = ctx.get_layer(layer_id)
    node = ctx.project.layerTreeRoot().findLayer(layer.id())
    if node is None:
        raise CommandError("unavailable", f"layer {layer.name()!r} is not in the layer tree")
    node.setItemVisibilityChecked(visible)
    return {"layer_id": layer.id(), "visible": bool(node.isVisible())}


@command("zoom_to_layer")
def zoom_to_layer(ctx: BridgeContext, layer_id: str) -> Dict[str, Any]:
    layer = ctx.get_layer(layer_id)
    _zoom(ctx, layer)
    return {"map_extent": extent_dict(ctx.get_map_extent(), ctx.map_crs())}
