"""Output tools: export_layer, render_map_image."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

from qgis.core import (
    QgsCoordinateTransform,
    QgsMapRendererParallelJob,
    QgsRasterLayer,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QEventLoop, QIODevice, QTimer
from qgis.PyQt.QtGui import QColor

from ..context import BridgeContext, clamp_limit, crs_id, extent_dict, layer_kind, parse_crs, transform_rect
from ..registry import CommandError, command

_VECTOR_FORMATS = {".gpkg": "GPKG", ".geojson": "GeoJSON", ".json": "GeoJSON", ".shp": "ESRI Shapefile"}
_FORMAT_ALIASES = {"gpkg": "GPKG", "geopackage": "GPKG", "geojson": "GeoJSON", "json": "GeoJSON",
                   "shp": "ESRI Shapefile", "shapefile": "ESRI Shapefile", "esri shapefile": "ESRI Shapefile"}
RENDER_TIMEOUT_MS = 55_000
MAX_PIXELS = 2048


@command("export_layer")
def export_layer(ctx: BridgeContext, layer_id: str, path: str, format: Optional[str] = None,
                 crs: Optional[str] = None, selected_only: bool = False) -> Dict[str, Any]:
    layer = ctx.get_layer(layer_id)
    if not isinstance(path, str) or not path:
        raise CommandError("invalid_params", "path must be a non-empty string")
    out = Path(path).expanduser()
    if not out.is_absolute():
        raise CommandError("invalid_params", "path must be absolute")
    dest_crs = parse_crs(crs) if crs else None
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise CommandError("invalid_params", f"cannot create directory {out.parent}: {e}") from None
    kind = layer_kind(layer)
    if kind == "vector":
        return _export_vector(ctx, layer, out, format, dest_crs, bool(selected_only))
    if kind == "raster":
        return _export_raster(layer, out, dest_crs)
    raise CommandError("invalid_params", f"cannot export {kind} layer {layer.name()!r}")


def _export_vector(ctx, layer: QgsVectorLayer, out: Path, fmt: Optional[str], dest_crs,
                   selected_only: bool) -> Dict[str, Any]:
    if fmt:
        driver = _FORMAT_ALIASES.get(fmt.lower(), fmt)
    else:
        driver = _VECTOR_FORMATS.get(out.suffix.lower())
        if driver is None:
            raise CommandError("invalid_params", f"cannot infer format from {out.suffix!r}; use .gpkg, .geojson, "
                                                 ".json, .shp or pass format")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = driver
    opts.fileEncoding = "UTF-8"
    opts.onlySelectedFeatures = selected_only
    if dest_crs is not None and dest_crs != layer.crs():
        opts.ct = QgsCoordinateTransform(layer.crs(), dest_crs, ctx.project)
    res = QgsVectorFileWriter.writeAsVectorFormatV3(layer, str(out), ctx.project.transformContext(), opts)
    err, msg = res[0], res[1]
    if err != QgsVectorFileWriter.WriterError.NoError:
        raise CommandError("unavailable", f"export failed: {msg or err}")
    count = layer.selectedFeatureCount() if selected_only else layer.featureCount()
    written = res[2] if len(res) > 2 and res[2] else str(out)
    return {"path": written, "format": driver, "feature_count": count}


def _export_raster(layer: QgsRasterLayer, out: Path, dest_crs) -> Dict[str, Any]:
    if out.suffix.lower() not in (".tif", ".tiff"):
        raise CommandError("invalid_params", "raster layers can only be exported to .tif/.tiff (GeoTIFF)")
    from osgeo import gdal

    gdal.UseExceptions()
    src = layer.source()
    try:
        if dest_crs is not None and dest_crs != layer.crs():
            ds = gdal.Warp(str(out), src, format="GTiff", dstSRS=dest_crs.toWkt(),
                           creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
        else:
            ds = gdal.Translate(str(out), src, format="GTiff",
                                creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
    except Exception as e:
        raise CommandError("unavailable", f"raster export failed (only GDAL-readable sources): {e}") from None
    if ds is None:
        raise CommandError("unavailable", "raster export failed")
    ds = None
    return {"path": str(out), "format": "GTiff", "feature_count": None}


@command("render_map_image")
def render_map_image(ctx: BridgeContext, width: int = 1024, height: int = 768, extent: Optional[dict] = None,
                     layer_ids: Optional[List[str]] = None, dpi: int = 96) -> Dict[str, Any]:
    width = clamp_limit(width, 1024, MAX_PIXELS)
    height = clamp_limit(height, 768, MAX_PIXELS)
    dpi = clamp_limit(dpi, 96, 600)
    layers = None
    if layer_ids is not None:
        if not isinstance(layer_ids, list):
            raise CommandError("invalid_params", "layer_ids must be a list")
        layers = [ctx.get_layer(r) for r in layer_ids]
    ms = ctx.get_map_settings(width, height, layers)
    ms.setOutputDpi(dpi)
    if ctx.canvas is None:
        ms.setBackgroundColor(QColor(255, 255, 255))
    if extent is not None:
        if not isinstance(extent, dict):
            raise CommandError("invalid_params", "extent must be an object with xmin,ymin,xmax,ymax[,crs]")
        from ..context import parse_bbox

        rect = parse_bbox([extent.get(k) for k in ("xmin", "ymin", "xmax", "ymax")])
        ms.setExtent(transform_rect(rect, parse_crs(extent.get("crs"), crs_id(ms.destinationCrs())),
                                    ms.destinationCrs()))
    if ms.extent().isEmpty() or ms.extent().isNull():
        raise CommandError("unavailable", "nothing to render: no layers or empty extent")

    job = QgsMapRendererParallelJob(ms)
    loop = QEventLoop()
    job.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    job.start()
    timer.start(RENDER_TIMEOUT_MS)
    if job.isActive():
        loop.exec()
    timer.stop()
    if job.isActive():
        job.cancelWithoutBlocking()
        raise CommandError("unavailable", f"rendering exceeded {RENDER_TIMEOUT_MS // 1000} s and was cancelled")
    image = job.renderedImage()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    buf.close()
    return {"png_base64": base64.b64encode(bytes(ba)).decode("ascii"),
            "width": image.width(), "height": image.height(),
            "extent": extent_dict(ms.visibleExtent(), ms.destinationCrs()),
            "layers": [l.name() for l in ms.layers()]}
