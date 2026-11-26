"""Coordinate transforms between a run's CRS (the imagery grid) and the map canvas destination CRS.

Shared by the draw/run side (``extract.py``, ``map_tools.py``) and the review side.  Every zoom, centre and
hit-test goes through these helpers so a Web Mercator project with UTM imagery (or two different UTM zones)
lands on the right ground.

All functions take the canvas (or its destination CRS) and the source CRS as an authid or WKT string, a
``QgsCoordinateReferenceSystem`` or None (None means "already in the destination CRS").
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
                       QgsRectangle)


def as_crs(crs: Any) -> Optional[QgsCoordinateReferenceSystem]:
    """An authid / WKT string or a CRS object as a valid ``QgsCoordinateReferenceSystem``, else None."""
    if crs is None or crs == "":
        return None
    c = crs if isinstance(crs, QgsCoordinateReferenceSystem) else QgsCoordinateReferenceSystem(str(crs))
    return c if c.isValid() else None


def canvas_crs(canvas: Any) -> QgsCoordinateReferenceSystem:
    """The map's destination CRS (what rubber bands, extents and click points are in)."""
    return canvas.mapSettings().destinationCrs()


def transformer(src: Any, dst: Any) -> Optional[QgsCoordinateTransform]:
    """A transform ``src`` -> ``dst`` or None when nothing needs doing (same CRS, or either one unknown)."""
    s, d = as_crs(src), as_crs(dst)
    if s is None or d is None or s == d:
        return None
    return QgsCoordinateTransform(s, d, QgsProject.instance())


def to_canvas_transform(canvas: Any, src: Any) -> Optional[QgsCoordinateTransform]:
    return transformer(src, canvas_crs(canvas))


def from_canvas_transform(canvas: Any, dst: Any) -> Optional[QgsCoordinateTransform]:
    return transformer(canvas_crs(canvas), dst)


def transform_geometry(geom: QgsGeometry, src: Any, dst: Any) -> QgsGeometry:
    """A transformed COPY of ``geom`` (the input is never modified)."""
    g = QgsGeometry(geom)
    tr = transformer(src, dst)
    if tr is not None:
        g.transform(tr)
    return g


def transform_point(pt: QgsPointXY, src: Any, dst: Any) -> QgsPointXY:
    tr = transformer(src, dst)
    return QgsPointXY(tr.transform(QgsPointXY(pt))) if tr is not None else QgsPointXY(pt)


def transform_rect(rect: QgsRectangle, src: Any, dst: Any) -> QgsRectangle:
    """Bounding box of ``rect`` in ``dst`` (densified, so it holds for rotated / non-linear projections)."""
    tr = transformer(src, dst)
    if tr is None:
        return QgsRectangle(rect)
    return tr.transformBoundingBox(rect)


def to_canvas_geometry(canvas: Any, geom: QgsGeometry, src: Any) -> QgsGeometry:
    return transform_geometry(geom, src, canvas_crs(canvas))


def to_canvas_point(canvas: Any, pt: QgsPointXY, src: Any) -> QgsPointXY:
    return transform_point(pt, src, canvas_crs(canvas))


def to_canvas_rect(canvas: Any, rect: QgsRectangle, src: Any) -> QgsRectangle:
    return transform_rect(rect, src, canvas_crs(canvas))


def from_canvas_geometry(canvas: Any, geom: QgsGeometry, dst: Any) -> QgsGeometry:
    return transform_geometry(geom, canvas_crs(canvas), dst)


def from_canvas_point(canvas: Any, pt: QgsPointXY, dst: Any) -> QgsPointXY:
    return transform_point(pt, canvas_crs(canvas), dst)


def to_canvas_points(canvas: Any, pts: Iterable[QgsPointXY], src: Any) -> List[QgsPointXY]:
    tr = to_canvas_transform(canvas, src)
    return [QgsPointXY(tr.transform(QgsPointXY(p))) if tr is not None else QgsPointXY(p) for p in pts]


def zoom_to_rect(canvas: Any, rect: QgsRectangle, src: Any, margin: float = 1.15) -> QgsRectangle:
    """Fit the canvas to ``rect`` given in ``src``: transform to the destination CRS first, then zoom."""
    r = to_canvas_rect(canvas, rect, src)
    if r.isNull() or r.isEmpty():
        return r
    r.scale(margin)
    canvas.setExtent(r)
    canvas.refresh()
    return r


def centre_on(canvas: Any, pt: QgsPointXY, src: Any) -> QgsPointXY:
    """Pan the canvas so ``pt`` (in ``src``) is centred, keeping the scale."""
    c = to_canvas_point(canvas, pt, src)
    canvas.setCenter(c)
    canvas.refresh()
    return c
