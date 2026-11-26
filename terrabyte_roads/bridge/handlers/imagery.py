"""Imagery tools: search_imagery, download_imagery, add_basemap.

Source: NAIP (and other COG collections with an ``image`` asset) from the Microsoft
Planetary Computer STAC API, subject to that service's terms and limits.
Signed URLs / SAS tokens are never returned, logged or stored in task records.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from qgis.core import (
    QgsContrastEnhancement,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMultiBandColorRenderer,
    QgsRasterLayer,
    QgsRasterMinMaxOrigin,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QUrl

from ...core import paths, stac
from ...core.tasks import ManagedTask
from ...core.types import TaskCancelled
from ..context import BridgeContext, clamp_limit, crs_id, parse_bbox
from ..registry import CommandError, command

MAX_PIXELS = 60_000_000
MAX_BBOX_SPAN_DEG = 1.0
ASSET_KEY = "image"
COMPRESSION_RATIO = 0.55

BASEMAP_PRESETS: Dict[str, Dict[str, Any]] = {
    "osm": {
        "name": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "zmin": 0,
        "zmax": 19,
        "attribution": "© OpenStreetMap contributors",
    },
}

GDAL_CONFIG = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "VSI_CACHE": "TRUE",
}


def _check_date(name: str, value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        raise CommandError("invalid_params", f"{name} must be YYYY-MM-DD") from None
    return str(value)


def _check_collection(collection: Any) -> str:
    try:
        return stac.check_collection(collection)
    except ValueError as e:
        raise CommandError("invalid_params", str(e)) from None


def _wgs84_bbox(bbox: Any) -> QgsRectangle:
    r = parse_bbox(bbox)
    if r.xMinimum() < -180 or r.xMaximum() > 180 or r.yMinimum() < -90 or r.yMaximum() > 90:
        raise CommandError("invalid_params", "bbox must be WGS84 [lon_min, lat_min, lon_max, lat_max] "
                                             "within [-180,-90,180,90]")
    if r.width() > MAX_BBOX_SPAN_DEG or r.height() > MAX_BBOX_SPAN_DEG:
        raise CommandError("invalid_params", f"bbox is larger than {MAX_BBOX_SPAN_DEG} degree; "
                                             "use a smaller area")
    return r


def _stac_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except stac.StacError as e:
        raise CommandError("unavailable", f"Planetary Computer request failed: {e}") from None
    except ValueError as e:
        raise CommandError("invalid_params", str(e)) from None


def _scrub(text: Any) -> str:
    return stac.redact(text)[:300]


@contextmanager
def scoped_gdal_config(options: Dict[str, str]):
    """Set thread-local GDAL config options for the duration of the block and restore them."""
    from osgeo import gdal

    old = {k: gdal.GetThreadLocalConfigOption(k, None) for k in options}
    try:
        for k, v in options.items():
            gdal.SetThreadLocalConfigOption(k, v)
        yield
    finally:
        for k, v in old.items():
            gdal.SetThreadLocalConfigOption(k, v)


@command("search_imagery")
def search_imagery(ctx: BridgeContext, bbox: List[float], start_date: Optional[str] = None,
                   end_date: Optional[str] = None, collection: str = "naip",
                   max_results: int = 10) -> Dict[str, Any]:
    r = parse_bbox(bbox)
    if r.xMinimum() < -180 or r.xMaximum() > 180 or r.yMinimum() < -90 or r.yMaximum() > 90:
        raise CommandError("invalid_params", "bbox must be WGS84 [lon_min, lat_min, lon_max, lat_max]")
    start, end = _check_date("start_date", start_date), _check_date("end_date", end_date)
    collection = _check_collection(collection)
    n = clamp_limit(max_results, 10, 50)
    items = _stac_call(stac.search, [r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()],
                       start, end, collection, n)
    return {"collection": collection, "provider": stac.PROVIDER, "items": items,
            "returned": len(items), "attribution": stac.ATTRIBUTION, "terms_note": stac.TERMS_NOTE}


def _output_path(ctx: BridgeContext, output_path: Optional[str], item_id: str, bbox: List[float]) -> Path:
    if output_path:
        p = Path(str(output_path)).expanduser()
        if p.suffix.lower() not in (".tif", ".tiff"):
            raise CommandError("invalid_params", "output_path must end with .tif or .tiff")
        if p.exists():
            raise CommandError("invalid_params", f"output_path already exists: {p} (choose another name)")
        return p
    digest = hashlib.sha1(("%s|%s" % (item_id, ",".join(f"{v:.6f}" for v in bbox))).encode()).hexdigest()[:8]
    fname = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', item_id).lstrip('.') or 'item'}_{digest}.tif"
    proj_file = ctx.project.fileName()
    if proj_file:
        return Path(proj_file).parent / "imagery" / fname
    return paths.data_dir() / "imagery" / fname


class DownloadImageryTask(ManagedTask):
    """Clip a remote COG to a window (native CRS and resolution) into a local GeoTIFF."""

    kind = "imagery_download"

    def __init__(self, *, collection: str, item_id: str, href: str, native_bbox: List[float],
                 out_path: Path, bands: Optional[List[int]], layer_name: str, year: Any):
        super().__init__(f"Download imagery {item_id}")
        self.collection, self.item_id, self.href = collection, item_id, href
        self.native_bbox = native_bbox
        self.out_path, self.bands = Path(out_path), bands
        self.layer_name, self.year = layer_name, year

    def work(self) -> Dict[str, Any]:
        from osgeo import gdal

        self.report(0.0, "signing request")
        src_path = "/vsicurl/" + stac.sign_href(self.href, self.collection)
        part = self.out_path.with_name(self.out_path.name + ".part")
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with scoped_gdal_config({**GDAL_CONFIG, "GDAL_HTTP_USERAGENT": stac.user_agent()}):
                self.check_cancelled()
                ds = gdal.OpenEx(src_path, gdal.OF_RASTER | gdal.OF_READONLY)
                if ds is None:
                    raise RuntimeError("could not open the remote image: " + _scrub(gdal.GetLastErrorMsg()))
                gt = ds.GetGeoTransform()
                if gt[2] or gt[4]:
                    raise RuntimeError("rotated rasters are not supported")
                px, py = gt[1], -gt[5]
                xmin, ymin, xmax, ymax = self.native_bbox
                x0 = max(0, int(math.floor((xmin - gt[0]) / px + 1e-6)))
                x1 = min(ds.RasterXSize, int(math.ceil((xmax - gt[0]) / px - 1e-6)))
                y0 = max(0, int(math.floor((gt[3] - ymax) / py + 1e-6)))
                y1 = min(ds.RasterYSize, int(math.ceil((gt[3] - ymin) / py - 1e-6)))
                if x1 <= x0 or y1 <= y0:
                    raise RuntimeError("the requested area does not overlap the image")
                if (x1 - x0) * (y1 - y0) > MAX_PIXELS * 1.1:
                    raise RuntimeError("requested window exceeds the pixel limit")
                self.report(0.02, "downloading")

                def progress(frac, _msg, _data):
                    self.report(0.02 + 0.96 * frac, "downloading")
                    return 0 if self.is_cancelled() else 1

                opts = dict(
                    format="GTiff", srcWin=[x0, y0, x1 - x0, y1 - y0],
                    creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES", "BIGTIFF=IF_SAFER"],
                    callback=progress)
                if self.bands:
                    opts["bandList"] = list(self.bands)
                out = None
                try:
                    out = gdal.Translate(str(part), ds, options=gdal.TranslateOptions(**opts))
                except RuntimeError as e:
                    if not self.is_cancelled():
                        raise RuntimeError(_scrub(e)) from None
                if self.is_cancelled():
                    out = None
                    raise TaskCancelled()
                if out is None:
                    raise RuntimeError("GDAL download failed: " + _scrub(gdal.GetLastErrorMsg()))
                out.FlushCache()
                out = None
                ds = None
            os.replace(part, self.out_path)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            raise
        self.report(1.0, "downloaded")
        return {"path": str(self.out_path)}

    def on_success(self, result: Dict[str, Any]) -> Dict[str, Any]:
        path = result["path"]
        layer = QgsRasterLayer(path, self.layer_name, "gdal")
        if not layer.isValid():
            raise RuntimeError(f"downloaded raster could not be loaded: {path}")
        from qgis.core import QgsProject

        n = layer.bandCount()
        if n >= 3:
            _apply_rgb(layer)
        QgsProject.instance().addMapLayer(layer)
        ext = layer.extent()
        return {
            "layer_id": layer.id(), "path": path, "width": layer.width(), "height": layer.height(),
            "band_count": n,
            "resolution_m": round(layer.rasterUnitsPerPixelX(), 4),
            "crs": crs_id(layer.crs()),
            "size_mb": round(os.path.getsize(path) / 1e6, 2),
            "item_id": self.item_id, "collection": self.collection,
            "attribution": stac.ATTRIBUTION,
            "extent": [round(ext.xMinimum(), 3), round(ext.yMinimum(), 3),
                       round(ext.xMaximum(), 3), round(ext.yMaximum(), 3)],
        }


def _apply_rgb(layer: QgsRasterLayer) -> None:
    """Bands 1,2,3 as RGB with a 2-98 % cumulative-cut stretch."""
    r = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)
    origin = QgsRasterMinMaxOrigin()
    origin.setLimits(QgsRasterMinMaxOrigin.Limits.CumulativeCut)
    origin.setCumulativeCutLower(0.02)
    origin.setCumulativeCutUpper(0.98)
    origin.setExtent(QgsRasterMinMaxOrigin.Extent.WholeRaster)
    r.setMinMaxOrigin(origin)
    for setter in (r.setRedContrastEnhancement, r.setGreenContrastEnhancement, r.setBlueContrastEnhancement):
        ce = QgsContrastEnhancement(layer.dataProvider().dataType(1))
        ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum)
        setter(ce)
    layer.setRenderer(r)


def _parse_bands(bands: Any) -> Optional[List[int]]:
    if bands is None:
        return None
    if not isinstance(bands, (list, tuple)) or not bands:
        raise CommandError("invalid_params", "bands must be a non-empty list of 1-based band numbers")
    try:
        out = [int(b) for b in bands]
    except (TypeError, ValueError):
        raise CommandError("invalid_params", "bands must be integers") from None
    if any(b < 1 or b > 4 for b in out) or len(set(out)) != len(out):
        raise CommandError("invalid_params", "bands must be unique numbers between 1 and 4")
    return out


@command("download_imagery")
def download_imagery(ctx: BridgeContext, item_id: str, bbox: List[float], collection: str = "naip",
                     output_path: Optional[str] = None, name: Optional[str] = None,
                     bands: Optional[List[int]] = None) -> Dict[str, Any]:
    if not isinstance(item_id, str) or not item_id:
        raise CommandError("invalid_params", "item_id must be a non-empty string")
    collection = _check_collection(collection)
    req = _wgs84_bbox(bbox)
    band_list = _parse_bands(bands)
    item = _stac_call(stac.get_item, collection, item_id)
    props = item.get("properties") or {}
    gsd = props.get("gsd")
    epsg = stac.item_epsg(props)
    ibox = item.get("bbox") or []
    if not isinstance(gsd, (int, float)) or gsd <= 0 or not epsg or len(ibox) < 4:
        raise CommandError("unavailable", f"item {item_id!r} lacks gsd/epsg/bbox metadata")
    try:
        href = stac.asset_href(item, ASSET_KEY)
    except stac.StacError as e:
        raise CommandError("invalid_params", str(e)) from None

    clipped = QgsRectangle(max(req.xMinimum(), ibox[0]), max(req.yMinimum(), ibox[1]),
                           min(req.xMaximum(), ibox[2]), min(req.yMaximum(), ibox[3]))
    if clipped.xMinimum() >= clipped.xMaximum() or clipped.yMinimum() >= clipped.yMaximum():
        raise CommandError("invalid_params", f"bbox does not overlap item {item_id!r} "
                                             f"(item bbox {[round(v, 5) for v in ibox[:4]]})")
    native_crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
    if not native_crs.isValid():
        raise CommandError("unavailable", f"unknown item CRS EPSG:{epsg}")
    tr = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), native_crs, ctx.project)
    nat = tr.transformBoundingBox(clipped)
    width = max(1, int(math.ceil(nat.width() / gsd)))
    height = max(1, int(math.ceil(nat.height() / gsd)))
    if width * height > MAX_PIXELS:
        side_km = math.sqrt(MAX_PIXELS) * gsd / 1000.0
        raise CommandError(
            "invalid_params",
            f"requested area is about {width}x{height} px ({width * height / 1e6:.0f} M pixels per band) at "
            f"{gsd} m; the limit is {MAX_PIXELS // 1_000_000} M. Use a smaller bbox "
            f"(roughly under {side_km:.1f} x {side_km:.1f} km).")
    nbands = len(band_list) if band_list else 4
    est_mb = round(width * height * nbands * COMPRESSION_RATIO / 1e6, 1)

    out_path = _output_path(ctx, output_path, item_id, [req.xMinimum(), req.yMinimum(),
                                                         req.xMaximum(), req.yMaximum()])
    year = props.get("naip:year") or str(props.get("datetime") or "")[:4]
    layer_name = name or (f"NAIP {year} {item_id}" if collection == "naip" else f"{collection} {item_id}")
    task = DownloadImageryTask(
        collection=collection, item_id=item_id, href=href,
        native_bbox=[nat.xMinimum(), nat.yMinimum(), nat.xMaximum(), nat.yMaximum()],
        out_path=out_path, bands=band_list, layer_name=layer_name, year=year)
    task_id = ctx.tasks.submit(task)
    return {"task_id": task_id, "status": "queued", "estimated_size_mb": est_mb,
            "width": width, "height": height}


def _xyz_uri(url: str, zmin: int, zmax: int) -> str:
    enc = bytes(QUrl.toPercentEncoding(url, b"/:?=&")).decode("ascii")
    return f"type=xyz&url={enc}&zmin={zmin}&zmax={zmax}"


@command("add_basemap")
def add_basemap(ctx: BridgeContext, name: str = "osm", url: Optional[str] = None,
                layer_name: Optional[str] = None) -> Dict[str, Any]:
    if url:
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")) \
                or "{z}" not in url or "{x}" not in url or not ("{y}" in url or "{-y}" in url):
            raise CommandError("invalid_params", "url must be an http(s) XYZ template containing {z}, {x} and {y}")
        preset: Dict[str, Any] = {"name": "XYZ basemap", "url": url, "zmin": 0, "zmax": 19, "attribution": ""}
    else:
        key = str(name or "osm").strip().lower()
        if key not in BASEMAP_PRESETS:
            raise CommandError("invalid_params", f"unknown basemap {name!r}; presets: {sorted(BASEMAP_PRESETS)} "
                                                 "(or pass url= with an XYZ template)")
        preset = BASEMAP_PRESETS[key]
    title = layer_name or preset["name"]
    layer = QgsRasterLayer(_xyz_uri(preset["url"], preset["zmin"], preset["zmax"]), title, "wms")
    if not layer.isValid():
        raise CommandError("unavailable", "could not create the XYZ layer")
    ctx.project.addMapLayer(layer, False)
    ctx.project.layerTreeRoot().addLayer(layer)
    if preset["attribution"]:
        try:
            layer.serverProperties().setAttribution(preset["attribution"])
        except AttributeError:
            pass
    return {"layer_id": layer.id(), "name": layer.name(), "attribution": preset["attribution"]}
