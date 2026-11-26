"""True ground metres for lengths, pixel sizes and tile sizes, whatever the imagery's coordinate system.

A geographic raster (EPSG:4326, degrees) has a different ground size east-west and north-south, and both change
with latitude, so one "metres per pixel" number is wrong.  Geographic and Web Mercator coordinates are measured on
the ellipsoid; other projected systems are planar and scaled by their map unit (metres, feet).
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence, Tuple

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsDistanceArea, QgsPointXY, Qgis

_METRES_PER_UNIT = {Qgis.DistanceUnit.Meters: 1.0, Qgis.DistanceUnit.Feet: 0.3048, Qgis.DistanceUnit.FeetUSSurvey: 1200.0 / 3937.0,
                    Qgis.DistanceUnit.NauticalMiles: 1852.0, Qgis.DistanceUnit.Kilometers: 1000.0,
                    Qgis.DistanceUnit.Yards: 0.9144, Qgis.DistanceUnit.Miles: 1609.344,
                    Qgis.DistanceUnit.Centimeters: 0.01, Qgis.DistanceUnit.Millimeters: 0.001}


def _crs(crs) -> QgsCoordinateReferenceSystem:
    if isinstance(crs, QgsCoordinateReferenceSystem):
        return crs
    c = QgsCoordinateReferenceSystem(str(crs))
    return c if c.isValid() else QgsCoordinateReferenceSystem.fromWkt(str(crs))


def _use_ellipsoid(c: QgsCoordinateReferenceSystem) -> bool:
    return (not c.isValid()) or c.isGeographic() or c.authid() in ("EPSG:3857", "EPSG:900913")


def _da(c: QgsCoordinateReferenceSystem) -> QgsDistanceArea:
    da = QgsDistanceArea()
    da.setSourceCrs(c, QgsCoordinateTransformContext())
    da.setEllipsoid(c.ellipsoidAcronym() if (c.isValid() and c.isGeographic() and c.ellipsoidAcronym()) else "WGS84")
    return da


def unit_factor(crs) -> float:
    """Metres per map unit for a projected CRS (1.0 for metres)."""
    c = _crs(crs)
    return _METRES_PER_UNIT.get(c.mapUnits(), 1.0) if c.isValid() else 1.0


def line_length_m(coords_map: Iterable[Sequence[float]], crs) -> float:
    """Ground length in metres of a path given as (x, y) pairs in ``crs``."""
    pts = [QgsPointXY(float(p[0]), float(p[1])) for p in coords_map]
    if len(pts) < 2:
        return 0.0
    c = _crs(crs)
    if _use_ellipsoid(c):
        da = _da(c)
        return float(sum(da.measureLine(a, b) for a, b in zip(pts, pts[1:])))
    k = unit_factor(c)
    return float(sum(math.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in zip(pts, pts[1:]))) * k


def ground_pixel_size_m(crs, x: float, y: float, pixel_size: float) -> Tuple[float, float]:
    """``(east_west_m, north_south_m)`` ground size of one pixel at map position (x, y); ``pixel_size`` is the
    pixel size in map units."""
    c = _crs(crs)
    if _use_ellipsoid(c):
        da = _da(c)
        p = QgsPointXY(float(x), float(y))
        return (float(da.measureLine(p, QgsPointXY(p.x() + pixel_size, p.y()))),
                float(da.measureLine(p, QgsPointXY(p.x(), p.y() - pixel_size))))
    k = unit_factor(c) * abs(pixel_size)
    return (k, k)


def mean_pixel_size_m(crs, x: float, y: float, pixel_size: float) -> float:
    """One number for captions ("56 cm imagery"): the mean of the two ground sizes."""
    ew, ns = ground_pixel_size_m(crs, x, y, pixel_size)
    return (ew + ns) / 2.0


def tile_size_m(crs, x: float, y: float, pixel_size: float, tile_px: int = 512) -> Tuple[float, float]:
    """``(width_m, height_m)`` of a tile of ``tile_px`` pixels at (x, y)."""
    ew, ns = ground_pixel_size_m(crs, x, y, pixel_size)
    return ew * tile_px, ns * tile_px


def area_km2(coords_ring: Sequence[Sequence[float]], crs) -> float:
    """Ground area in km2 of a polygon ring in ``crs``."""
    from qgis.core import QgsGeometry
    c = _crs(crs)
    g = QgsGeometry.fromPolygonXY([[QgsPointXY(float(p[0]), float(p[1])) for p in coords_ring]])
    if _use_ellipsoid(c):
        return float(_da(c).measureArea(g)) / 1e6
    return g.area() * unit_factor(c) ** 2 / 1e6
