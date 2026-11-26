"""Imagery checks, the run's tile grid and tile reading (all local; nothing is sent from here).

``inspect_layer`` checks a raster layer and describes it as the file has it; ``at_resolution`` gives the imagery as a
run at the chosen resolution sees it.  A tile is 512 x 512 pixels AT THE RUN'S RESOLUTION.  ``compute_grid`` aligns
the grid to the raster's pixels, with its top-left corner snapped to a pixel; a rotated raster gets a north-up grid
over its footprint.  ``area_km2`` is the ground area the tiles cover; ``drawn_km2`` is the ground area of the drawn
shape on the imagery (the two differ because tiles reach past the shape's edges).  There is ONE mapping between run
pixels and the map, the grid's ``origin`` and square ``pixel_size`` (``AreaChoice.tile_rect`` for tiling,
``RunRef.pixel_to_map`` for lines), so a typed resolution changes the scale but never where a pixel is on the map.
When the raster may mark pixels as empty (nodata, alpha, a mask) the tile count is an upper bound until the run has
read the tiles and dropped those that are all one colour.

Tiles are read with GDAL (``read_tile_rgb``): at the image's own resolution an exact crop (a rotated raster is warped
into the tile's square), otherwise built from area means of the source pixels; pixels past the image are black.  The
means are computed here from a plain full-resolution read (no GDAL resampling, no overviews), so every QGIS gets the
same bytes: GDAL's warper ``average`` fills a partly covered edge pixel in 3.13 and leaves it empty in 3.8.
"""
from __future__ import annotations

import dataclasses
import math
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.numbers import count as _count

TILE_PX = 512
COARSE_GSD_M = 1.0

OK, ONLINE, NO_POSITION, NOT_COLOUR, COARSE, INVALID = "ok", "online", "nopos", "notcolour", "coarse", "invalid"


@dataclass
class ImageryInfo:
    layer_id: str
    name: str
    source: str
    provider: str
    kind: str = OK
    message: str = ""
    crs: str = ""
    extent: Tuple[float, float, float, float] = (0, 0, 0, 0)
    width: int = 0
    height: int = 0
    pixel_size: float = 0.0
    gsd_m: float = 0.0
    gsd_y_m: float = 0.0
    bands: int = 0
    footprint: Tuple[Tuple[float, float], ...] = ()
    geotransform: Tuple[float, ...] = ()
    raster_size: Tuple[int, int] = (0, 0)
    may_hold_empty: bool = False
    gsd_in_file: bool = True
    image_gsd_m: float = 0.0
    scale: float = 1.0
    image_from: str = "file"
    target_choice: str = "image"

    @property
    def usable(self) -> bool:
        return self.kind in (OK, COARSE, NOT_COLOUR)

    @property
    def rotated(self) -> bool:
        return bool(self.footprint)

    @property
    def tile_side_m(self) -> float:
        return TILE_PX * self.gsd_m

    @property
    def tile_h_m(self) -> float:
        return TILE_PX * (self.gsd_y_m or self.gsd_m)


def gsd_label(gsd_m: float) -> str:
    cm = gsd_m * 100.0
    return f"{cm:.0f} cm" if cm >= 10 or abs(cm - round(cm)) < 0.05 else f"{cm:.1f} cm"


def number_text(v: float, decimals: int = 1) -> str:
    """'10', '15.5', '2': at most ``decimals`` decimals, no trailing zeros."""
    t = f"{round(float(v), decimals):.{decimals}f}"
    return t.rstrip("0").rstrip(".") if "." in t else t


def res_label(m: float) -> str:
    """A resolution as the Resolution card and the summary line say it: '10 cm', '15.5 cm', '60 cm' below 1 m,
    '1 m', '2 m', '1.5 m' from 1 m."""
    if m >= 1.0 - 1e-9:
        return f"{number_text(m, 2)} m"
    return f"{number_text(m * 100.0, 1)} cm"


def side_label(metres: float) -> str:
    """'51', '307', '1,024' (a tile's side in metres; one decimal under 10 m)."""
    return f"{metres:,.0f}" if metres >= 10 else f"{metres:.1f}"


def is_rotated(gt) -> bool:
    """The geotransform turns the raster.  Rotation terms below a millionth of the pixel (rounding noise some
    tools write: under a tenth of a pixel across 100,000 pixels) count as none, so such a raster stays an exact crop."""
    try:
        return max(abs(gt[2]), abs(gt[4])) > 1e-6 * max(abs(gt[1]), abs(gt[5]), 1e-12)
    except (TypeError, IndexError):
        return False


def is_square(gt) -> bool:
    """A north-up raster's pixels are square: |gt[5]| is gt[1] to a millionth (the noise ``is_rotated`` allows)."""
    try:
        return abs(abs(gt[5]) - abs(gt[1])) <= 1e-6 * max(abs(gt[1]), abs(gt[5]), 1e-12)
    except (TypeError, IndexError):
        return True


SAME_SCALE = 1e-9


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= SAME_SCALE * max(abs(a), abs(b), 1e-300)


def _metres_per_unit_xy(crs, extent) -> Tuple[float, float]:
    """Ground metres per map unit east-west and north-south at the raster centre.  Equal (and 1.0 for metres)
    in a projected CRS; in degrees they differ (a degree of longitude shrinks with latitude)."""
    try:
        if crs.authid() in ("EPSG:3857", "EPSG:900913"):
            from ..ui.flow import ground

            cx, cy = (extent[0] + extent[2]) / 2.0, (extent[1] + extent[3]) / 2.0
            ew, ns = ground.ground_pixel_size_m(crs, cx, cy, 1.0)
            return max(ew, 1e-6), max(ns, 1e-6)
        if crs.isGeographic():
            lat = math.radians((extent[1] + extent[3]) / 2.0)
            my = 111132.92 - 559.82 * math.cos(2 * lat) + 1.175 * math.cos(4 * lat)
            mx = 111412.84 * math.cos(lat) - 93.5 * math.cos(3 * lat)
            return max(mx, 1e-6), my
        from qgis.core import Qgis, QgsUnitTypes
        f = QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), Qgis.DistanceUnit.Meters)
        return f, f
    except Exception:
        return 1.0, 1.0


def _metres_per_unit(crs, extent) -> float:
    return _metres_per_unit_xy(crs, extent)[0]


def inspect_layer(layer: Any) -> ImageryInfo:
    """Classify a QgsRasterLayer for Road Extractor (file rasters only; XYZ/WMS are never offered).  Coarse and
    non-RGB rasters are classified but still usable."""
    from osgeo import gdal

    info = ImageryInfo(layer_id=layer.id(), name=layer.name(), source=layer.source(),
                       provider=layer.providerType())
    if not layer.isValid():
        info.kind, info.message = INVALID, "This layer could not be read."
        return info
    if info.provider != "gdal":
        info.kind = ONLINE
        return info
    src = layer.source().split("|")[0]
    info.source = src
    info.crs = layer.crs().authid() or layer.crs().toWkt()
    ext = layer.extent()
    info.extent = (ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum())
    info.width, info.height = layer.width(), layer.height()
    info.bands = layer.bandCount()
    size, empty, own_crs = (info.width, info.height), False, True
    try:
        gdal.UseExceptions()
        ds = gdal.Open(src)
        gt = tuple(ds.GetGeoTransform())
        own_crs = bool(ds.GetProjection())
        has_srs = own_crs or bool(ds.GetGCPCount())
        size = (ds.RasterXSize, ds.RasterYSize)
        used = [1, 2, 3] if ds.RasterCount >= 3 else [1]
        empty = any(ds.GetRasterBand(b).GetMaskFlags() != gdal.GMF_ALL_VALID for b in used)
        ds = None
    except Exception:
        gt, has_srs = (0, 1, 0, 0, 0, 1), False
    if (gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0) or gt == (0, 1, 0, 0, 0, 1)) and not has_srs \
            or not layer.crs().isValid():
        info.kind = NO_POSITION
        info.message = "This raster has no position on the map, so it can't run."
        return info
    info.geotransform, info.raster_size, info.may_hold_empty = gt, size, bool(empty)
    if is_rotated(gt):
        w, h = size
        info.footprint = tuple((gt[0] + i * gt[1] + j * gt[2], gt[3] + i * gt[4] + j * gt[5])
                               for i, j in ((0, 0), (w, 0), (w, h), (0, h)))
        xs, ys = [p[0] for p in info.footprint], [p[1] for p in info.footprint]
        info.extent = (min(xs), min(ys), max(xs), max(ys))
        info.width, info.height = w, h
        px = math.hypot(gt[1], gt[4])
        info.pixel_size = px
        mx, my = _metres_per_unit_xy(layer.crs(), info.extent)
        info.gsd_m = px * mx
        info.gsd_y_m = px * my
    else:
        px = abs(gt[1]) if gt[1] else (ext.width() / max(1, info.width))
        info.pixel_size = px
        mx, my = _metres_per_unit_xy(layer.crs(), info.extent)
        info.gsd_m = px * mx
        info.gsd_y_m = (abs(gt[5]) if gt[5] else px) * my
    info.gsd_in_file = bool(own_crs) and math.isfinite(info.gsd_m) and info.gsd_m > 0
    if info.gsd_m > COARSE_GSD_M:
        info.kind = COARSE
    return info


def _ns_over_ew(info: ImageryInfo) -> float:
    """Ground metres per map unit north-south over east-west (1 in a projected CRS; not 1 in degrees)."""
    gt = info.geotransform
    if info.gsd_m <= 0 or info.pixel_size <= 0:
        return 1.0
    py = info.pixel_size if (info.rotated or not gt or not gt[5]) else abs(gt[5])
    ns = (info.gsd_y_m or info.gsd_m) / py
    ew = info.gsd_m / info.pixel_size
    return ns / ew if ew > 0 and ns > 0 else 1.0


def at_resolution(info: ImageryInfo, image_m: float, target_m: float) -> ImageryInfo:
    """The imagery as a run at ``target_m`` ground metres per pixel sees it, the image's own pixel being ``image_m``
    (the file's ``gsd_m``, or what the user typed).  The grid's pixel is the image's own map pixel times
    ``target_m / image_m``: ``image_m`` sets the factor, never where a pixel is on the map.  At the image's own
    resolution of a square-pixel raster with the file's value this is an unchanged copy of ``info``."""
    if not (image_m > 0 and target_m > 0 and math.isfinite(image_m) and math.isfinite(target_m)):
        raise ValueError("a resolution must be a positive number of metres")
    s = target_m / image_m
    if _same(s, 1.0):
        s = 1.0
    out = dataclasses.replace(info)
    square = info.rotated or not info.geotransform or is_square(info.geotransform)
    if s == 1.0 and image_m == info.gsd_m and square:
        return out
    ratio = _ns_over_ew(info)
    out.scale = s
    out.image_gsd_m = image_m
    out.pixel_size = info.pixel_size * s if s != 1.0 else info.pixel_size
    out.gsd_m = target_m if s != 1.0 else image_m
    out.gsd_y_m = out.gsd_m * ratio
    return out


def wavefront_order(tiles: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Tiles in run order: sorted by ``row + col`` from the top-left, then by row."""
    return sorted((tuple(t) for t in tiles), key=lambda rc: (rc[0] + rc[1], rc[0]))


@dataclass
class AreaChoice:
    geometry_wkt: str
    crs: str
    origin: Tuple[float, float]
    pixel_size: float
    rows: int
    cols: int
    tiles: List[Tuple[int, int]]
    area_km2: float
    gsd_m: float
    source: str = "drawn"
    tile_px: int = TILE_PX
    gsd_y_m: float = 0.0
    drawn_km2: float = 0.0
    upper_bound: bool = False
    image_gsd_m: float = 0.0
    image_from: str = "file"
    target_choice: str = "image"

    @property
    def image_m(self) -> float:
        """The image's own resolution the run took (``gsd_m`` is the resolution the run runs at)."""
        return self.image_gsd_m or self.gsd_m

    @property
    def native(self) -> bool:
        """The run runs at the image's own resolution."""
        return _same(self.gsd_m, self.image_m)

    @property
    def tile_count(self) -> int:
        return len(self.tiles)

    def tile_km2(self) -> float:
        """Ground area of one tile."""
        return (self.tile_px * self.gsd_m / 1000.0) * (self.tile_px * (self.gsd_y_m or self.gsd_m) / 1000.0)

    @property
    def whole_image(self) -> bool:
        """The area is the raster's full extent ("Use whole image")."""
        return self.source == "raster"

    @property
    def order(self) -> List[Tuple[int, int]]:
        return wavefront_order(self.tiles)

    def tile_side_map(self) -> float:
        return self.tile_px * self.pixel_size

    def tile_rect(self, row: int, col: int) -> Tuple[float, float, float, float]:
        s = self.tile_side_map()
        x0 = self.origin[0] + col * s
        y1 = self.origin[1] - row * s
        return (x0, y1 - s, x0 + s, y1)


MAX_TILES_HARD = 4096
RUN_TILE_LIMIT = MAX_TILES_HARD


def whole_image_geometry(info: ImageryInfo):
    """The image as a polygon in the imagery CRS: what "Use whole image" feeds ``compute_grid`` in place of a dragged
    rectangle, so the tiling is the one a drag over the whole raster gives.  The raster's extent, or for a rotated
    raster its four-corner footprint (the image, not the box around it)."""
    from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle

    if info.footprint:
        pts = [QgsPointXY(x, y) for x, y in info.footprint]
        return QgsGeometry.fromPolygonXY([pts + [pts[0]]])
    return QgsGeometry.fromRect(QgsRectangle(*info.extent))


class _ImageCover:
    """Which squares of the north-up grid hold pixels of a rotated image.

    A tile is read by warping the image into its square; a pixel of the tile gets imagery when its centre falls on the
    image (GDAL's warper: the centre maps to a source position inside the raster).  So a tile holds imagery exactly when
    one of its 512 x 512 pixel centres lies inside the footprint.  A tile wholly inside does; for one on the edge the
    overlap decides at once unless it is a thin sliver (a convex region whose area over its length is 2.2 pixels or
    more holds a pixel centre: its width is at least that, and a convex region of lattice width over 1 + 2/sqrt(3)
    holds a lattice point); a sliver is tested centre by centre.

    At another resolution than the image's own the tile is warped from the image's cells (``scale`` source pixels a
    side, each the area mean of the pixels under it), so the same test runs on the cells: their geotransform is the
    file's with the pixel vectors times ``scale``, and there are ceil(columns / scale) x ceil(rows / scale) of them."""

    SURE_WIDTH_PX = 2.2

    def __init__(self, info: ImageryInfo):
        from qgis.core import QgsGeometry, QgsPointXY

        gt = info.geotransform
        w, h = info.raster_size
        s = info.scale or 1.0
        if s != 1.0:
            gt = (gt[0], gt[1] * s, gt[2] * s, gt[3], gt[4] * s, gt[5] * s)
            w, h = _cells(w, s), _cells(h, s)
        self.gt = gt
        self.w, self.h = w, h
        self.det = gt[1] * gt[5] - gt[2] * gt[4]
        if s == 1.0:
            self.foot = whole_image_geometry(info)
        else:
            pts = [QgsPointXY(gt[0] + i * gt[1] + j * gt[2], gt[3] + i * gt[4] + j * gt[5])
                   for i, j in ((0, 0), (w, 0), (w, h), (0, h))]
            self.foot = QgsGeometry.fromPolygonXY([pts + [pts[0]]])
        self.engine = QgsGeometry.createGeometryEngine(self.foot.constGet())
        self.engine.prepareGeometry()

    def holds_imagery(self, rect, px: float) -> bool:
        """``rect`` (a QgsRectangle of TILE_PX pixels of ``px`` map units) has a pixel centre on the image."""
        from qgis.core import QgsGeometry

        tg = QgsGeometry.fromRect(rect)
        if self.engine.contains(tg.constGet()):
            return True
        if not self.engine.intersects(tg.constGet()):
            return False
        inter = self.foot.intersection(tg)
        if inter.isNull() or inter.isEmpty():
            return False
        bb = inter.boundingBox()
        diag = math.hypot(bb.width(), bb.height()) / px
        if diag > 0 and (inter.area() / (px * px)) / diag >= self.SURE_WIDTH_PX:
            return True
        n = TILE_PX
        k0 = max(0, int(math.floor((bb.xMinimum() - rect.xMinimum()) / px - 1)))
        k1 = min(n - 1, int(math.ceil((bb.xMaximum() - rect.xMinimum()) / px)))
        l0 = max(0, int(math.floor((rect.yMaximum() - bb.yMaximum()) / px - 1)))
        l1 = min(n - 1, int(math.ceil((rect.yMaximum() - bb.yMinimum()) / px)))
        if k1 < k0 or l1 < l0:
            return False
        xx, yy = np.meshgrid(rect.xMinimum() + (np.arange(k0, k1 + 1) + 0.5) * px,
                             rect.yMaximum() - (np.arange(l0, l1 + 1) + 0.5) * px)
        g = self.gt
        dx, dy = xx - g[0], yy - g[3]
        u = (g[5] * dx - g[2] * dy) / self.det
        v = (g[1] * dy - g[4] * dx) / self.det
        return bool(((u >= 0) & (u < self.w) & (v >= 0) & (v < self.h)).any())


def _cells(n: int, scale: float) -> int:
    """How many cells of ``scale`` source pixels cover ``n`` pixels (the last one may be partly on the image)."""
    return max(1, int(math.ceil(n / scale - 1e-9)))


def _ground_km2(info: ImageryInfo, geometry) -> float:
    """Ground area of ``geometry`` (imagery CRS) in km2, with the same metres-per-unit as the tile area, so a shape
    that is exactly its tiles has exactly their area."""
    px = info.pixel_size
    if px <= 0:
        return 0.0
    mx = info.gsd_m / px
    my = (info.gsd_y_m or info.gsd_m) / px
    return max(0.0, geometry.area() * mx * my / 1e6)


def compute_grid(info: ImageryInfo, geometry, source: str = "drawn", max_tiles: int = 200000) -> Optional[AreaChoice]:
    """Tiles of ``info`` that intersect ``geometry`` (a QgsGeometry in the imagery CRS), clipped to the image (its
    extent, or its footprint when rotated): the tiles the run sends (see the module notes)."""
    from qgis.core import QgsGeometry, QgsRectangle

    if geometry is None or geometry.isNull() or geometry.isEmpty() or info.pixel_size <= 0:
        return None
    ext = QgsRectangle(*info.extent)
    g = geometry.intersection(whole_image_geometry(info))
    if g.isNull() or g.isEmpty() or g.area() <= 0:
        return AreaChoice(geometry.asWkt(), info.crs, (ext.xMinimum(), ext.yMaximum()), info.pixel_size,
                          0, 0, [], 0.0, info.gsd_m, source, gsd_y_m=info.gsd_y_m,
                          image_gsd_m=info.image_gsd_m or info.gsd_m, image_from=info.image_from,
                          target_choice=info.target_choice)
    bb = g.boundingBox()
    px = info.pixel_size
    x0 = info.extent[0] + math.floor((bb.xMinimum() - info.extent[0]) / px + 1e-9) * px
    y0 = info.extent[3] - math.floor((info.extent[3] - bb.yMaximum()) / px + 1e-9) * px
    side = TILE_PX * px
    cols = max(1, int(math.ceil((bb.xMaximum() - x0) / side - 1e-9)))
    rows = max(1, int(math.ceil((y0 - bb.yMinimum()) / side - 1e-9)))
    engine = QgsGeometry.createGeometryEngine(g.constGet())
    engine.prepareGeometry()
    cover = _ImageCover(info) if info.footprint else None
    tiles: List[Tuple[int, int]] = []
    if rows * cols > max_tiles * 4:
        tiles = [(r, c) for r in range(rows) for c in range(cols)]
    else:
        for r in range(rows):
            for c in range(cols):
                rect = QgsRectangle(x0 + c * side, y0 - (r + 1) * side, x0 + (c + 1) * side, y0 - r * side)
                tg = QgsGeometry.fromRect(rect)
                if engine.intersects(tg.constGet()) and (cover is None or cover.holds_imagery(rect, px)):
                    tiles.append((r, c))
    a = AreaChoice(g.asWkt(), info.crs, (x0, y0), px, rows, cols, tiles, 0.0, info.gsd_m, source,
                   gsd_y_m=info.gsd_y_m, drawn_km2=round(_ground_km2(info, g), 6),
                   upper_bound=bool(info.may_hold_empty and tiles), image_gsd_m=info.image_gsd_m or info.gsd_m,
                   image_from=info.image_from, target_choice=info.target_choice)
    a.area_km2 = round(len(tiles) * a.tile_km2(), 4)
    return a


def tile_has_imagery(arr: np.ndarray) -> bool:
    """False for a tile that is entirely one colour (black padding, nodata): such a tile is not sent and not charged.
    ``arr`` is the tile as it is sent (``read_tile_rgb``)."""
    return bool(arr.size) and bool((arr != arr[0, 0]).any())


def keep_tiles(area: AreaChoice, keep) -> None:
    """The run has read the tiles and only ``keep`` hold imagery: the area becomes those tiles, an exact count."""
    keep = set(keep)
    area.tiles = [t for t in area.tiles if tuple(t) in keep]
    area.area_km2 = round(len(area.tiles) * area.tile_km2(), 4)
    area.upper_bound = False


def tile_cost_label(tiles: int) -> str:
    """'1 tile', '36 tiles', '4,096 tiles' (singular for one)."""
    return f"{_count(tiles)} tile" + ("" if tiles == 1 else "s")


def area_tiles_label(area: AreaChoice) -> str:
    """'36 tiles', or 'up to 49 tiles' while some of them may hold no imagery."""
    t = tile_cost_label(area.tile_count)
    return f"up to {t}" if getattr(area, "upper_bound", False) else t


def km2_label(km2: float) -> str:
    """'2.4 km²'; two decimals under 0.1 km² so a small area never reads as 0.0."""
    if km2 >= 0.095 or km2 <= 0:
        return f"{km2:,.1f} km²"
    return f"{km2:.2f} km²" if km2 >= 0.005 else "<0.01 km²"


STEP_SAMPLES = 1 << 18


def _finish(steps):
    """Run a stepwise reader (``tile_rgb_steps``, ``tile_png_steps``) to its end and return its result."""
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


def read_tile_rgb(source: str, area: AreaChoice, row: int, col: int) -> np.ndarray:
    """512 x 512 x 3 uint8 crop of the raster for tile (row, col); black beyond the raster edge.  A rotated raster is
    warped into the tile's square, so the tile holds the imagery that lies under it on the map.

    The tile's rectangle is the grid's (``area.tile_rect``) and the file's own geotransform says which source pixels
    lie under it.  At the image's own pixel (the grid's ``pixel_size`` is the file's, square pixels) the tile is a
    direct crop; otherwise it is built from area means of the image's cells, ``scale`` source pixels a side and
    anchored at the raster's first pixel (``_north_up_cells_tile``, ``_rotated_cells_tile``).  The work is done by
    ``tile_rgb_steps``; this runs it to the end at once."""
    return _finish(tile_rgb_steps(source, area, row, col))


def tile_rgb_steps(source: str, area: AreaChoice, row: int, col: int):
    """``read_tile_rgb`` in steps: a generator that does the same work, the same way, and yields after each
    piece of at most ``STEP_SAMPLES`` source samples (a read of the file and the means of the cells it covers), so the
    run can hand the interface back between pieces and act on Stop at once; it returns the tile.  The pieces never
    change a pixel: each cell's sum is taken over the same source pixels in the same order whatever the pieces."""
    from osgeo import gdal

    gdal.UseExceptions()
    ds = gdal.Open(source)
    gt = ds.GetGeoTransform()
    W, H = ds.RasterXSize, ds.RasterYSize
    xmin, ymin, xmax, ymax = area.tile_rect(row, col)
    rect = (xmin, ymin, xmax, ymax)
    if is_rotated(gt):
        s = area.pixel_size / math.hypot(gt[1], gt[4])
        if not _same(s, 1.0):
            out = yield from _rotated_cells_tile(ds, gt, rect, s)
            ds = None
            return out
    elif not (is_square(gt) and _same(area.pixel_size, abs(gt[1]))):
        out = yield from _north_up_cells_tile(ds, gt, rect)
        ds = None
        return out
    if is_rotated(gt):
        warped = gdal.Warp("", ds, format="MEM", outputBounds=(xmin, ymin, xmax, ymax), width=TILE_PX,
                           height=TILE_PX, resampleAlg="bilinear")
        nb = ds.RasterCount
        bands = [1, 2, 3] if nb >= 3 else [1, 1, 1]
        arrs = []
        for b in bands:
            band = warped.GetRasterBand(b)
            arrs.append(_to_u8(band.ReadAsArray(), gdal.GetDataTypeName(band.DataType)))
        warped = ds = None
        return np.ascontiguousarray(np.stack(arrs, axis=-1))
    px0 = int(round((xmin - gt[0]) / gt[1]))
    py0 = int(round((ymax - gt[3]) / gt[5]))
    out = np.zeros((TILE_PX, TILE_PX, 3), dtype=np.uint8)
    sx0, sy0 = max(0, px0), max(0, py0)
    sx1, sy1 = min(W, px0 + TILE_PX), min(H, py0 + TILE_PX)
    if sx1 > sx0 and sy1 > sy0:
        nb = ds.RasterCount
        bands = [1, 2, 3] if nb >= 3 else [1, 1, 1]
        arrs = []
        for b in bands:
            band = ds.GetRasterBand(b)
            a = band.ReadAsArray(sx0, sy0, sx1 - sx0, sy1 - sy0)
            arrs.append(_to_u8(a, gdal.GetDataTypeName(band.DataType)))
        out[sy0 - py0:sy1 - py0, sx0 - px0:sx1 - px0, :] = np.stack(arrs, axis=-1)
    ds = None
    return out


def _tile_bands(ds) -> List[int]:
    return [1, 2, 3] if ds.RasterCount >= 3 else [1, 1, 1]


def _axis_sums(a: np.ndarray, b: np.ndarray, axis: int) -> np.ndarray:
    """Sums of ``a`` (2-D) along ``axis`` between consecutive positions ``b`` (pixel units within 0 to the axis' length,
    monotonic): a pixel counts in proportion to the part of it between the two positions.  Float64."""
    if b[0] > b[-1]:
        return np.flip(_axis_sums(a, b[::-1], axis), axis=axis)
    n = a.shape[axis]
    m = len(b) - 1
    acc = np.uint64 if a.dtype.kind in "ui" else np.float64
    bi = np.rint(b)
    if np.all(np.abs(b - bi) < 1e-9):
        bi = bi.astype(np.int64)
        step = int(bi[1] - bi[0])
        if step > 0 and bool(np.all(np.diff(bi) == step)):
            lo, hi = int(bi[0]), int(bi[-1])
            if axis == 1:
                return a[:, lo:hi].reshape(a.shape[0], m, step).sum(axis=2, dtype=acc).astype(np.float64)
            return a[lo:hi, :].reshape(m, step, a.shape[1]).sum(axis=1, dtype=acc).astype(np.float64)
    fl = np.clip(np.floor(b).astype(np.int64), 0, n - 1)
    frac = b - fl
    if axis == 1:
        whole = np.add.reduceat(a, fl, axis=1, dtype=np.float64)[:, :m] * (fl[1:] > fl[:-1])[None, :]
        return whole + frac[1:][None, :] * a[:, fl[1:]] - frac[:-1][None, :] * a[:, fl[:-1]]
    out = np.empty((m, a.shape[1]))
    for j in range(m):
        f0, f1 = int(fl[j]), int(fl[j + 1])
        row = a[f0:f1].sum(axis=0, dtype=acc).astype(np.float64) if f1 > f0 else np.zeros(a.shape[1])
        out[j] = row + frac[j + 1] * a[f1] - frac[j] * a[f0]
    return out


SNAP_PX = 1e-6


def _snap(b: np.ndarray) -> np.ndarray:
    """Edges within a millionth of a pixel (SNAP_PX) of a whole pixel are that whole pixel.  The map-to-pixel arithmetic
    leaves noise of a few billionths of a pixel at large map coordinates, which would send cells of a whole-number scale
    down the fractional path and nudge a mean that lies exactly half-way between two levels below it; snapped, every
    cell of a whole-number scale is the exact mean of its whole pixels, however the read is split into pieces."""
    r = np.rint(b)
    return np.where(np.abs(b - r) < SNAP_PX, r, b)


def _cell_means(ds, bands: List[int], xb: np.ndarray, yb: np.ndarray):
    """The area means of ``bands`` over cells: column edges ``xb`` and row edges ``yb`` (source pixel units,
    monotonic, either direction).  Each source pixel weighs the part of it inside the cell; only the part of a cell on
    the image counts.  A generator: it reads the bands at full resolution, together, one block of whole
    cells at a time (at most ``STEP_SAMPLES`` source samples: whole cell rows, or part of one cell row for a very large
    factor), yields after each block, and returns one (rows, cols) float64 array of means per band and where a cell
    holds any of the image.  No GDAL resampling, no overviews."""
    W, H = ds.RasterXSize, ds.RasterYSize
    xc, yc = _snap(np.clip(xb, 0.0, float(W))), _snap(np.clip(yb, 0.0, float(H)))
    wx, wy = np.abs(np.diff(xc)), np.abs(np.diff(yc))
    ny, nx = len(wy), len(wx)
    covered = (wy[:, None] > 0) & (wx[None, :] > 0)
    sums = [np.zeros((ny, nx)) for _ in bands]
    if not covered.any():
        return sums, covered
    one_type = len({ds.GetRasterBand(b).DataType for b in bands}) == 1
    rows_per_cell = max(1.0, abs(float(yc[-1] - yc[0])) / max(1, ny)) + 1
    cols_per_cell = max(1.0, abs(float(xc[-1] - xc[0])) / max(1, nx)) + 1
    per_row = cols_per_cell * nx * rows_per_cell * len(bands)
    if per_row <= STEP_SAMPLES:
        k, kx = max(1, int(STEP_SAMPLES // per_row)), nx
    else:
        k, kx = 1, max(1, int(STEP_SAMPLES // (cols_per_cell * rows_per_cell * len(bands))))
    for j0 in range(0, ny, k):
        j1 = min(ny, j0 + k)
        lo, hi = min(yc[j0], yc[j1]), max(yc[j0], yc[j1])
        rs, re = int(math.floor(lo)), min(H, int(math.ceil(hi)))
        if re <= rs:
            continue
        for i0 in range(0, nx, kx):
            i1 = min(nx, i0 + kx)
            xs = xc[i0:i1 + 1]
            c0, c1 = int(math.floor(xs.min())), min(W, int(math.ceil(xs.max())))
            if c1 <= c0:
                continue
            if one_type and len(bands) > 1:
                block = ds.ReadAsArray(c0, rs, c1 - c0, re - rs, band_list=list(bands))
                block = block.reshape(len(bands), re - rs, c1 - c0)
            else:
                block = [ds.GetRasterBand(b).ReadAsArray(c0, rs, c1 - c0, re - rs) for b in bands]
            for i in range(len(bands)):
                down = _axis_sums(block[i], yc[j0:j1 + 1] - rs, axis=0)
                sums[i][j0:j1, i0:i1] = _axis_sums(down, xs - c0, axis=1)
            block = down = None
            yield
    area = wy[:, None] * wx[None, :]
    return [np.divide(sm, area, out=np.zeros_like(sm), where=area > 0) for sm in sums], covered


def _as_band_type(means: np.ndarray, dtype) -> np.ndarray:
    """Means back in the band's own type (rounded half up for whole-number types), so ``_to_u8`` treats them as it
    treats the band's own pixels."""
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        return np.clip(np.floor(means + 0.5), info.min, info.max).astype(dtype)
    return means


def _band_kind(ds, b: int):
    from osgeo import gdal, gdal_array

    band = ds.GetRasterBand(b)
    return np.dtype(gdal_array.GDALTypeCodeToNumericTypeCode(band.DataType)), gdal.GetDataTypeName(band.DataType)


def _north_up_cells_tile(ds, gt, rect):
    """A north-up raster's tile at another resolution than its own (or with non-square pixels): every tile pixel is
    the area mean of the source pixels under it (``_cell_means``); pixels past the image are black.  A generator."""
    xmin, ymin, xmax, ymax = rect
    k = np.arange(TILE_PX + 1, dtype=np.float64) / TILE_PX
    xb = (xmin + (xmax - xmin) * k - gt[0]) / gt[1]
    yb = (ymax - (ymax - ymin) * k - gt[3]) / gt[5]
    out = np.zeros((TILE_PX, TILE_PX, 3), dtype=np.uint8)
    bands = _tile_bands(ds)
    uniq = sorted(set(bands))
    means, covered = yield from _cell_means(ds, uniq, xb, yb)
    if not covered.any():
        return out
    for i, b in enumerate(bands):
        dt, name = _band_kind(ds, b)
        out[:, :, i] = np.where(covered, _to_u8(_as_band_type(means[uniq.index(b)], dt), name), 0)
    return np.ascontiguousarray(out)


def _rotated_cells_tile(ds, gt, rect, s: float):
    """A rotated raster's tile at another resolution than its own: the image's cells (``s`` source pixels a side,
    anchored at its first pixel) under the tile are taken as area means, then warped into the tile's square with
    bilinear, as the image's own pixels are at its own resolution.  A generator."""
    from osgeo import gdal

    W, H = ds.RasterXSize, ds.RasterYSize
    nw, nh = _cells(W, s), _cells(H, s)
    xmin, ymin, xmax, ymax = rect
    det = gt[1] * gt[5] - gt[2] * gt[4]
    us, vs = [], []
    for x, y in ((xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)):
        dx, dy = x - gt[0], y - gt[3]
        us.append((gt[5] * dx - gt[2] * dy) / det)
        vs.append((gt[1] * dy - gt[4] * dx) / det)
    i0 = max(0, int(math.floor(min(us) / s)) - 1)
    i1 = min(nw, int(math.ceil(max(us) / s)) + 1)
    j0 = max(0, int(math.floor(min(vs) / s)) - 1)
    j1 = min(nh, int(math.ceil(max(vs) / s)) + 1)
    out = np.zeros((TILE_PX, TILE_PX, 3), dtype=np.uint8)
    if i1 <= i0 or j1 <= j0:
        return out
    xb = np.arange(i0, i1 + 1, dtype=np.float64) * s
    yb = np.arange(j0, j1 + 1, dtype=np.float64) * s
    bands = _tile_bands(ds)
    uniq = sorted(set(bands))
    means, _covered = yield from _cell_means(ds, uniq, xb, yb)
    mem = gdal.GetDriverByName("MEM").Create("", i1 - i0, j1 - j0, len(uniq), gdal.GDT_Float64)
    mem.SetGeoTransform((gt[0] + i0 * s * gt[1] + j0 * s * gt[2], gt[1] * s, gt[2] * s,
                         gt[3] + i0 * s * gt[4] + j0 * s * gt[5], gt[4] * s, gt[5] * s))
    mem.SetProjection(ds.GetProjection())
    for k, m in enumerate(means):
        mem.GetRasterBand(k + 1).WriteArray(m)
    warped = gdal.Warp("", mem, format="MEM", outputBounds=rect, width=TILE_PX, height=TILE_PX,
                       resampleAlg="bilinear")
    for i, b in enumerate(bands):
        dt, name = _band_kind(ds, b)
        out[:, :, i] = _to_u8(_as_band_type(warped.GetRasterBand(uniq.index(b) + 1).ReadAsArray(), dt), name)
    warped = mem = None
    return np.ascontiguousarray(out)


def _to_u8(a: np.ndarray, dtype_name: str) -> np.ndarray:
    if a.dtype == np.uint8:
        return a
    if a.dtype == np.uint16:
        return (a // 257).astype(np.uint8)
    a = a.astype(np.float64)
    hi = float(a.max()) if a.size else 1.0
    return np.clip(a * (255.0 / hi if hi > 255 else 1.0), 0, 255).astype(np.uint8)


def encode_png_rgb(arr: np.ndarray) -> bytes:
    """Lossless 8-bit RGB PNG (no Qt, safe on any thread)."""
    h, w, _ = arr.shape
    raw = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def tile_png(source: str, area: AreaChoice, row: int, col: int) -> bytes:
    return _finish(tile_png_steps(source, area, row, col))


def tile_png_steps(source: str, area: AreaChoice, row: int, col: int):
    """``tile_png`` in steps: the tile's read in steps (``tile_rgb_steps``), then its PNG; returns the PNG."""
    arr = yield from tile_rgb_steps(source, area, row, col)
    yield
    return encode_png_rgb(arr)
