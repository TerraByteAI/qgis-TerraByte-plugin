"""ReviewSession: one run's lines, class and length filters, line delete and export (QGIS side).

The run result (lines in run-pixel coordinates) becomes an editable memory layer.  The layer
stays in edit mode for the whole review, so a delete is a single undo step on the layer's own undo stack.
Hidden lines (class toggles, minimum length) are filtered by the layer renderer through layer variables,
so the map follows live; the same rule is evaluated in Python for counts.
"""
from __future__ import annotations

import time
import weakref

from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsExpressionContextUtils, QgsFeature, QgsGeometry,
                       QgsInvertedPolygonRenderer, QgsMapLayer, QgsPointXY, QgsProject,
                       QgsRuleBasedRenderer, QgsVectorLayer, QgsFillSymbol, QgsRectangle,
                       QgsSingleSymbolRenderer)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor

from ...cloud.runref import RunRef
from ...core import export as export_mod
from ...core import paths as core_paths
from ...core.numbers import counted
from .. import theme
from . import crs_util, ground

GROUP_NAME = "TerraByte review"
DEBOUNCE_MS = 120
MIN_LINE_M = export_mod.MIN_LINE_M
FOREIGN_UNDO_S = 2.0
DEFAULT_OFF = (6,)


def default_layer_name() -> str:
    """``Roads YYYY-MM-DD HH-MM`` in local time."""
    return export_mod.default_layer_name()


def _log(msg: str) -> None:
    """A warning in the QGIS message log (Road Extractor tab) and the plugin's own log buffer."""
    from .controller import log

    log(msg, Qgis.MessageLevel.Warning)


def _crs(s: str) -> QgsCoordinateReferenceSystem:
    c = QgsCoordinateReferenceSystem(s)
    if not c.isValid():
        c = QgsCoordinateReferenceSystem.fromWkt(s)
    return c


class ReviewSession(QObject):
    changed = pyqtSignal()
    tool_state = pyqtSignal(bool)
    selection_changed = pyqtSignal(int)
    line_deleted = pyqtSignal(int)
    line_restored = pyqtSignal()
    closing = pyqtSignal()
    lost = pyqtSignal()

    def __init__(self, ctx, result: Dict[str, Any], summary: Optional[Dict[str, Any]] = None):
        super().__init__()
        self.ctx = ctx
        self.result = result
        self.summary = dict(summary or {})
        self.run_id = result.get("run_id") or self.summary.get("run_id") or ""
        ref = RunRef.from_client_ref(result.get("client_ref"))
        if ref is None:
            raise ValueError("This run has no map reference (client_ref), so its lines can't be placed on the map.")
        self.ref = ref
        self.crs = _crs(ref.crs)
        self.gsd = float(ref.gsd_m or 0.5)
        self.tiles_used = int((result.get("summary") or {}).get("tiles_used", self.summary.get("tiles_used", 0)) or 0)
        self.n_tiles = ref.rows * ref.cols
        run = getattr(ctx, "run", None)
        mine = run is not None and bool(self.run_id) and getattr(run, "run_id", None) == self.run_id
        self.run_state = str(getattr(run, "state", "done")) if mine else "done"
        self.stopped = self.run_state in ("stopped", "failed")
        self.tiles_charged = int(run.tiles_charged) if mine else (self.tiles_used or self.n_tiles)
        self.tiles_planned = int(getattr(run, "tiles_total", 0) or 0) if mine else 0
        self.tiles_ran = int(getattr(run, "tiles_done", 0) or 0) if mine else 0
        held = self.summary.get("tiles_held")
        self.tiles_held: Optional[int] = int(held) if isinstance(held, int) and not isinstance(held, bool) else None
        self.partial = self.tiles_held is not None and self.tiles_held < self.tiles_charged
        self.layer_name = default_layer_name()
        self.min_len_m = 0.0
        self.class_on: Dict[int, bool] = {c: c not in DEFAULT_OFF for c in range(1, 8)}
        self.export_name = default_layer_name()
        self.export_name_edited = False
        self.export_format = "GeoPackage"
        self.export_add_to_map = True
        self.export_folder = str(export_mod.default_folder(QgsProject.instance().fileName()))
        self.export_folder_edited = False
        self._selected: Optional[int] = None
        self._hover: Optional[int] = None
        self._bands: Dict[str, List[QgsRubberBand]] = {}
        self._canvas_hooked = False
        self.step = "keep"
        self.handler = None
        self.closed = False
        self._dirty = True
        self._lines: Dict[int, Dict[str, Any]] = {}
        self.tool = None
        self.prev_tool = None
        self.keys_wanted = False
        self._own_edit = False
        self._foreign_undo_at = -1e9
        self.group = None
        self.layers: Dict[str, QgsVectorLayer] = {}
        self._deb = QTimer(self)
        self._deb.setSingleShot(True)
        self._deb.setInterval(DEBOUNCE_MS)
        self._deb.timeout.connect(self._apply_filters)
        self._clip = self._area_clip()
        self._build_layers()
        self._layer_ids = [l.id() for l in list(self.layers.values()) + [self.layer]]
        self._watch_removal()
        self._load_lines(self._clip_lines(result.get("lines") or []))
        self._begin_editing()
        self._apply_filters()
        self._hook_canvas()

    def _area_clip(self) -> Optional[QgsGeometry]:
        """The drawn area (inside the raster), widened by one pixel.  Lines can also come back over the black
        padding that fills the grid beyond the raster edge; everything outside this area is dropped."""
        wkt = self.ref.area_wkt
        if not wkt:
            return None
        g = QgsGeometry.fromWkt(wkt)
        if g.isNull() or g.isEmpty():
            return None
        self._area_exact = g
        return g.buffer(abs(self.ref.pixel_size), 4)


    def _clip_lines(self, lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Clip every line to the drawn area; a line cut into pieces keeps its id on the first piece."""
        clip = self._clip
        if clip is None:
            return lines
        out: List[Dict[str, Any]] = []
        next_id = max([int(l["id"]) for l in lines] + [0]) + 1
        for ln in lines:
            pts = ln["points"]
            g = QgsGeometry.fromPolylineXY(self.to_map(pts))
            if clip.contains(g):
                out.append(ln)
                continue
            inter = g.intersection(clip)
            if inter.isNull() or inter.isEmpty():
                continue
            parts = []
            for sub in inter.asGeometryCollection():
                if sub.type() != Qgis.GeometryType.Line:
                    continue
                if sub.isMultipart():
                    parts.extend(sub.asMultiPolyline())
                else:
                    parts.append(sub.asPolyline())
            first = True
            for part in parts:
                if len(part) < 2:
                    continue
                px = [list(self.ref.map_to_pixel(p.x(), p.y())) for p in part]
                new = dict(ln)
                new["points"] = px
                if not first:
                    new["id"] = next_id
                    next_id += 1
                out.append(new)
                first = False
        return out

    def to_map(self, pts) -> List[QgsPointXY]:
        return [QgsPointXY(*self.ref.pixel_to_map(x, y)) for x, y in pts]

    def to_px(self, pts) -> np.ndarray:
        return np.array([self.ref.map_to_pixel(p.x(), p.y()) for p in pts], float)

    def px_len_m(self, px: np.ndarray) -> float:
        """True ground metres of a pixel path: measured on the ellipsoid for a geographic or Web Mercator run CRS
        (pixel sizes differ east-west and north-south there), planar in metres otherwise."""
        pts = np.asarray(px, float)
        if len(pts) < 2:
            return 0.0
        return ground.line_length_m([(p.x(), p.y()) for p in self.to_map(pts)], self.crs)


    def _uri(self, geom: str, fields: str) -> str:
        return f"{geom}?crs={self.crs.authid() or self.crs.toWkt()}&{fields}"

    def _register_transform(self) -> None:
        """When the run's coordinate system differs from the project's, record QGIS's default transformation
        between them in the project, so that reopening a project saved during the review never asks
        "Select Transformation" about a plugin layer.  Only when none is recorded yet; best effort."""
        try:
            from qgis.core import QgsCoordinateTransform, QgsDatumTransform

            prj = QgsProject.instance()
            src, dst = self.crs, prj.crs()
            if not src.isValid() or not dst.isValid() or src == dst:
                return
            tc = prj.transformContext()
            if tc.hasTransform(src, dst):
                return
            ops = [o for o in QgsDatumTransform.operations(src, dst) if o.isAvailable]
            if not ops:
                return
            tc.addCoordinateOperation(src, dst, ops[0].proj)
            prj.setTransformContext(tc)
        except Exception:
            pass

    def _build_layers(self) -> None:
        prj = QgsProject.instance()
        self._register_transform()
        self.layer = QgsVectorLayer(self._uri("LineString", "field=line_id:integer&field=class:string&"
                                              "field=class_id:integer&field=length_m:double"), self.layer_name + " (review)", "memory")
        self.layer.setCustomProperty("terrabyte_roads/review", True)
        prj.addMapLayer(self.layer, False)
        root = prj.layerTreeRoot()
        root.insertLayer(0, self.layer)
        self.group = root.insertGroup(0, GROUP_NAME)
        area = QgsGeometry.fromWkt(self.ref.area_wkt) if self.ref.area_wkt else QgsGeometry()
        if area.isNull() or area.isEmpty():
            x0, y0, _, _ = self.ref.tile_rect_map(self.ref.rows - 1, 0)
            _, _, x1, y1 = self.ref.tile_rect_map(0, self.ref.cols - 1)
            area = QgsGeometry.fromRect(QgsRectangle(x0, y0, x1, y1))
        self.area_geom = area
        edge = QgsVectorLayer(self._uri("Polygon", "field=id:integer"), "Area edge", "memory")
        f = QgsFeature(edge.fields())
        f.setGeometry(area)
        edge.dataProvider().addFeatures([f])
        edge.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
            {"color": "0,0,0,0", "outline_color": "247,245,251,102", "outline_width": "0.26"})))
        dim = QgsVectorLayer(self._uri("Polygon", "field=id:integer"), "Outside dimmed", "memory")
        f = QgsFeature(dim.fields())
        f.setGeometry(area)
        dim.dataProvider().addFeatures([f])
        inner = QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": "7,8,10,128", "outline_style": "no"}))
        dim.setRenderer(QgsInvertedPolygonRenderer(inner))
        self.layers.update(edge=edge, dim=dim)
        for lyr in (edge, dim):
            prj.addMapLayer(lyr, False)
            self.group.addLayer(lyr)
            lyr.setCustomProperty("terrabyte_roads/review", True)
        self.group.setCustomProperty("terrabyte_roads/review", True)
        self._style_lines(self.layer)

    def _watch_removal(self) -> None:
        prj = QgsProject.instance()
        prj.layersWillBeRemoved.connect(self._on_layers_removed)
        prj.layerTreeRoot().removedChildren.connect(self._on_children_removed)

    def _unwatch_removal(self) -> None:
        prj = QgsProject.instance()
        for sig, slot in ((prj.layersWillBeRemoved, self._on_layers_removed),
                          (prj.layerTreeRoot().removedChildren, self._on_children_removed)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _ours_leaving(self) -> bool:
        return self.closed

    def _on_layers_removed(self, ids) -> None:
        if self._ours_leaving() or not set(ids) & set(self._layer_ids):
            return
        self._announce_lost()

    def _on_children_removed(self, *_a) -> None:
        if self._ours_leaving() or self.group is None:
            return
        try:
            gone = self.group not in QgsProject.instance().layerTreeRoot().children()
        except RuntimeError:
            gone = True
        if gone:
            self._announce_lost()

    def _announce_lost(self) -> None:
        if getattr(self, "_lost_sent", False):
            return
        self._lost_sent = True
        QTimer.singleShot(0, lambda: None if self.closed else self.lost.emit())

    def _begin_editing(self) -> None:
        self.layer.startEditing()
        for sig in (self.layer.attributeValueChanged, self.layer.featuresDeleted, self.layer.featureAdded,
                    self.layer.geometryChanged, self.layer.afterRollBack):
            sig.connect(self._mark_dirty)
        self.layer.undoStack().indexChanged.connect(self._mark_dirty)
        self.layer.undoStack().indexChanged.connect(self._stack_moved)
        for sig in (self.layer.featuresDeleted, self.layer.featureAdded, self.layer.undoStack().indexChanged):
            sig.connect(self._layer_changed)

    def _stack_moved(self, *_a) -> None:
        if not self._own_edit:
            self._foreign_undo_at = time.monotonic()

    def undo_is_ours(self) -> bool:
        """True when nothing but the review moved its layer's undo stack in the last ``FOREIGN_UNDO_S`` seconds.  A
        Ctrl+Z whose press QGIS's own Undo took (it then acts on this layer) must not be undone a second time on its
        release."""
        return time.monotonic() - self._foreign_undo_at > FOREIGN_UNDO_S

    def _layer_changed(self, *a) -> None:
        """Whatever changed the review layer (the plugin's delete, its Undo, QGIS's Edit > Undo): tell the panel one
        turn later, once, so its counts are read from the layer again."""
        if getattr(self, "_change_pending", False) or self.closed:
            return
        self._change_pending = True
        ref = weakref.ref(self)

        def fire():
            me = ref()
            if me is None:
                return
            me._change_pending = False
            try:
                if me.closed:
                    return
                me._dirty = True
                me._check_selection()
                me._refresh_bands()
                me.changed.emit()
            except Exception:
                pass
        QTimer.singleShot(0, fire)

    def _mark_dirty(self, *a) -> None:
        self._dirty = True
        self._deb.start()

    def _style_lines(self, layer: QgsVectorLayer, variables: bool = True) -> None:
        """One rule per class with the palette's symbol (theme.line_symbol: dark casing under the coloured line, in
        screen pixels).  The review layer filters through its layer variables; the exported layer (``variables``
        False) shows every line.  Symbol levels draw all casings before any line, so joins have no dark seam."""
        vis = ("\"length_m\" >= @trb_min_len AND strpos(@trb_classes, '|' || \"class_id\" || '|') > 0"
               if variables else "TRUE")
        root = QgsRuleBasedRenderer.Rule(None)
        for cid, _k, name, _colour in theme.CLASSES:
            rule = QgsRuleBasedRenderer.Rule(theme.line_symbol(cid), 0, 0, f"\"class_id\" = {cid} AND {vis}", name)
            root.appendChild(rule)
        renderer = QgsRuleBasedRenderer(root)
        renderer.setUsingSymbolLevels(True)
        layer.setRenderer(renderer)
        if variables:
            self._push_variables(layer)

    def _push_variables(self, layer: QgsVectorLayer) -> None:
        on = "|" + "|".join(str(c) for c, v in self.class_on.items() if v) + "|"
        for k, v in (("trb_min_len", self.min_len_m), ("trb_classes", on)):
            QgsExpressionContextUtils.setLayerVariable(layer, k, v)


    @staticmethod
    def unique_lines(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """A tile delivered twice (a resumed run) must replace its lines, not add to them.  A line that repeats
        another exactly (same class, same points, either direction) is dropped, and line ids are made unique."""
        seen, ids, out, nxt = set(), set(), [], max([int(l["id"]) for l in lines] + [0]) + 1
        for ln in lines:
            pts = tuple((round(float(p[0]), 3), round(float(p[1]), 3)) for p in ln["points"])
            key = (int(ln["class_id"]), min(pts, pts[::-1]))
            if key in seen:
                continue
            seen.add(key)
            if int(ln["id"]) in ids:
                ln = dict(ln)
                ln["id"] = nxt
                nxt += 1
            ids.add(int(ln["id"]))
            out.append(ln)
        return out

    def _load_lines(self, lines: List[Dict[str, Any]]) -> None:
        lines = self.unique_lines(lines)
        feats = []
        for ln in lines:
            f = QgsFeature(self.layer.fields())
            pts = np.asarray(ln["points"], float)
            length = self.px_len_m(pts)
            if length < MIN_LINE_M:
                continue
            f.setGeometry(QgsGeometry.fromPolylineXY(self.to_map(pts)))
            cid = int(ln["class_id"])
            f.setAttributes([int(ln["id"]), theme.CLASS_NAMES.get(cid, f"class_{cid}"), cid, round(length, 2)])
            feats.append(f)
        self.layer.dataProvider().addFeatures(feats)
        self._next_id = max([int(l["id"]) for l in lines] + [0]) + 1
        self._dirty = True

    @property
    def lines(self) -> Dict[int, Dict[str, Any]]:
        if self._dirty:
            fresh = {}
            try:
                for f in self.layer.getFeatures():
                    lid = int(f["line_id"])
                    g = QgsGeometry(f.geometry())
                    fresh[lid] = {"fid": f.id(), "line_id": lid, "class_id": int(f["class_id"]),
                                  "length_m": float(f["length_m"]), "geom": g, "bbox": g.boundingBox()}
            except RuntimeError:
                self._announce_lost()
                self._dirty = False
                return self._lines
            self._lines = fresh
            self._dirty = False
        return self._lines

    def set_min_len(self, m: float) -> None:
        self.min_len_m = max(0.0, float(m))
        self._check_selection()
        self.changed.emit()
        self._deb.start()

    def set_class_on(self, cid: int, on: bool) -> None:
        self.class_on[int(cid)] = bool(on)
        self._check_selection()
        self.changed.emit()
        self._deb.start()

    def _check_selection(self) -> None:
        """A line the filters now hide can't stay selected."""
        ln = self.lines.get(self._selected) if self._selected is not None else None
        if self._selected is not None and (ln is None or not self.visible(ln)):
            self.deselect()

    def _apply_filters(self) -> None:
        """Push the filter state to the renderer (debounced: the map follows ~120 ms after the last change)."""
        if self.closed:
            return
        try:
            self._push_variables(self.layer)
            self.layer.triggerRepaint()
        except RuntimeError:
            self.closed = True

    def visible(self, ln) -> bool:
        return ln["length_m"] >= max(self.min_len_m, MIN_LINE_M) and self.class_on.get(ln["class_id"], True)

    def visible_lines(self) -> List[Dict[str, Any]]:
        return [l for l in self.lines.values() if self.visible(l)]

    def counts(self) -> Dict[str, Any]:
        ls = list(self.lines.values())
        base = [l for l in ls if l["length_m"] >= max(self.min_len_m, MIN_LINE_M)]
        len_hidden = len(ls) - len(base)
        km_by = {c: 0.0 for c in range(1, 8)}
        n_by = {c: 0 for c in range(1, 8)}
        for l in base:
            km_by[l["class_id"]] = km_by.get(l["class_id"], 0.0) + l["length_m"] / 1000.0
            n_by[l["class_id"]] = n_by.get(l["class_id"], 0) + 1
        vis = [l for l in base if self.class_on.get(l["class_id"], True)]
        return {"total": len(ls), "len_hidden": len_hidden,
                "km_by_class": km_by, "lines_by_class": n_by,
                "lines": len(vis), "km": sum(l["length_m"] for l in vis) / 1000.0,
                "all_by_class": {c: sum(1 for l in ls if l["class_id"] == c) for c in range(1, 8)}}

    @property
    def tiles_count(self) -> int:
        """Tiles this run was charged (the service's figure, also after a stop); the grid size when unknown."""
        return self.tiles_charged

    def tiles_words(self) -> str:
        """``3 tiles`` or, after a stopped or failed run, ``3 tiles charged``."""
        if self.partial:
            return counted(int(self.tiles_held), "tile", "tiles")
        n = self.tiles_charged
        return counted(n, "tile", "tiles") + (" charged" if self.stopped else "")

    def delete_line(self, line_id: Optional[int] = None) -> bool:
        if line_id is None:
            line_id = self._selected
        ln = self.lines.get(line_id) if line_id is not None else None
        if ln is None:
            return False
        if not self.layer.isEditable():
            self.layer.startEditing()
        self._own_edit = True
        try:
            self.layer.beginEditCommand("Delete line")
            ok = self.layer.deleteFeature(ln["fid"])
            if ok:
                self.layer.endEditCommand()
            else:
                self.layer.destroyEditCommand()
        finally:
            self._own_edit = False
        if not ok:
            return False
        self._dirty = True
        self._selected = self._hover = None
        self._refresh_bands()
        self.selection_changed.emit(-1)
        self.line_deleted.emit(int(line_id))
        self.changed.emit()
        return True

    @property
    def deleted_count(self) -> int:
        """Lines deleted and not brought back."""
        return int(self.layer.undoStack().index())

    def can_undo(self) -> bool:
        return self.layer.undoStack().canUndo()

    def undo(self) -> bool:
        if not self.can_undo():
            return False
        self._own_edit = True
        try:
            self.layer.undoStack().undo()
        finally:
            self._own_edit = False
        self._dirty = True
        self._selected = self._hover = None
        self._refresh_bands()
        self.selection_changed.emit(-1)
        self.line_restored.emit()
        self.changed.emit()
        return True

    def handle_action(self, name: str) -> None:
        """What the review tool's keys and the on-map buttons do."""
        if self.closed:
            return
        if name == "delete":
            self.delete_line()
        elif name == "undo":
            self.undo()
        elif name == "deselect":
            self.deselect()
        h = self.handler
        if h is not None:
            h(name)

    def selected_line(self) -> Optional[Dict[str, Any]]:
        return self.lines.get(self._selected) if self._selected is not None else None

    def selected_id(self) -> Optional[int]:
        return self._selected if self.selected_line() is not None else None

    def select_line(self, line_id: Optional[int]) -> None:
        ln = self.lines.get(line_id) if line_id is not None else None
        new = line_id if ln is not None else None
        if new != self._selected:
            self._selected = new
            self.selection_changed.emit(-1 if new is None else int(new))
        self._hover = None
        self._refresh_bands()

    def deselect(self) -> None:
        self.select_line(None)

    def hover_line(self, line_id: Optional[int]) -> None:
        if line_id != self._hover:
            self._hover = line_id
            self._refresh_bands()

    def pick(self, p: QgsPointXY, tolerance: float) -> Optional[int]:
        """Nearest visible line within ``tolerance`` (in the run's CRS; hidden lines can't be picked)."""
        best, bid = tolerance, None
        pt = QgsGeometry.fromPointXY(p)
        for l in self.lines.values():
            if not self.visible(l):
                continue
            bb = QgsRectangle(l["bbox"])
            bb.grow(tolerance)
            if not bb.contains(p):
                continue
            d = l["geom"].distance(pt)
            if d <= best:
                best, bid = d, l["line_id"]
        return bid

    def pick_at(self, canvas_pt: QgsPointXY, px: int = 12) -> Optional[int]:
        """Hit-test a click or hover given in the MAP CRS (what the canvas hands a map tool).  Both the point and
        the pixel tolerance are carried into the run's CRS, so it works when the two differ."""
        cv = self.canvas()
        try:
            p = crs_util.from_canvas_point(cv, canvas_pt, self.crs)
            m = cv.mapUnitsPerPixel() * px
            tol = max(p.distance(crs_util.from_canvas_point(cv, QgsPointXY(canvas_pt.x() + m, canvas_pt.y()), self.crs)),
                      p.distance(crs_util.from_canvas_point(cv, QgsPointXY(canvas_pt.x(), canvas_pt.y() + m), self.crs)))
        except Exception:
            return None
        return self.pick(p, tol)

    def line_anchor_canvas_px(self, line_id: int):
        """Pixel position (in the canvas widget) of the middle of a line, or None."""
        ln = self.lines.get(line_id)
        if ln is None:
            return None
        g = ln["geom"]
        mid = g.interpolate(g.length() / 2.0)
        if mid.isNull():
            return None
        cv = self.canvas()
        p = crs_util.to_canvas_point(cv, mid.asPoint(), self.crs)
        q = cv.getCoordinateTransform().transform(p)
        return int(q.x()), int(q.y())

    def selection_widths(self):
        """``(hover, edge, halo, core)`` pixel widths of the highlight.  Bands are drawn in screen pixels, so the look
        does not change with the map scale.  They are sized from 1 mm at the screen's DPI, so a short selected line is
        always clearly thicker and brighter than its neighbours, the widest of which is the highway with its casing
        (6 px).  The dark edge is the halo plus ``SELECT_EDGE_PX`` each side, so the near-white halo reads on a pale
        yellow street."""
        try:
            nb = 1.0 * self.canvas().logicalDpiX() / 25.4
        except (RuntimeError, AttributeError):
            nb = 3.8
        hover, halo, core = max(7, round(nb * 2.2)), max(14, round(nb * 3.6)), max(7, round(nb * 1.8))
        return hover, halo + round(2 * theme.SELECT_EDGE_PX), halo, core

    @staticmethod
    def hover_widths(class_id: int, hover: int):
        """``(halo, casing, line)`` pixel widths of the hover look of a line of ``class_id``: the line a little wider
        than on the map (0.5 to 1 px), over its dark casing, over a lighter halo that shows at least 2 px each side."""
        line = int(theme.line_width_px(class_id)) + 1
        casing = line + round(2 * theme.LINE_CASING_PX)
        return max(hover, casing + 4), casing, line

    def _dress(self, key: str, line_id: Optional[int]) -> None:
        """Give the ``key`` bands the class colour (and, for hover, the widths) of the line they are about to show."""
        ln = self.lines.get(line_id) if line_id is not None else None
        if ln is None:
            return
        cid = int(ln["class_id"])
        bands = self._bands[key]
        bands[-1].setColor(QColor(theme.class_colour(cid)))
        if key == "hover":
            for rb, w in zip(bands, self.hover_widths(cid, self.selection_widths()[0])):
                rb.setWidth(w)

    def _band(self, key: str, width: int, colour: QColor) -> QgsRubberBand:
        bands = self._bands.setdefault(key, [])
        rb = QgsRubberBand(self.canvas(), Qgis.GeometryType.Line)
        rb.setColor(colour)
        rb.setWidth(width)
        bands.append(rb)
        return rb

    def _set_band(self, bands: List[QgsRubberBand], line_id: Optional[int]) -> None:
        ln = self.lines.get(line_id) if line_id is not None else None
        for rb in bands:
            if ln is None:
                rb.reset(Qgis.GeometryType.Line)
            else:
                cv = self.canvas()
                rb.setToGeometry(crs_util.to_canvas_geometry(cv, ln["geom"], self.crs), crs_util.canvas_crs(cv))

    def _refresh_bands(self) -> None:
        if self.closed:
            return
        try:
            if not self._bands:
                hover, edge, halo, core = self.selection_widths()
                casing = core + round(2 * theme.LINE_CASING_PX)
                self._band("hover", hover, QColor(247, 245, 251, 190))
                self._band("hover", hover, QColor(theme.LINE_CASING))
                self._band("hover", hover, QColor(theme.TEXT_2))
                self._band("sel", edge, QColor(theme.SELECT_EDGE))
                self._band("sel", halo, QColor(theme.SELECT_HALO))
                self._band("sel", casing, QColor(theme.LINE_CASING))
                self._band("sel", core, QColor(theme.TEXT_2))
            hv = self._hover if self._hover != self._selected else None
            self._dress("hover", hv)
            self._dress("sel", self._selected)
            self._set_band(self._bands["hover"], hv)
            self._set_band(self._bands["sel"], self._selected)
        except RuntimeError:
            pass

    def _clear_bands(self) -> None:
        from qgis.PyQt import sip

        for bands in self._bands.values():
            for rb in bands:
                try:
                    rb.reset(Qgis.GeometryType.Line)
                    sc = rb.scene()
                    if sc is not None:
                        sc.removeItem(rb)
                    sip.delete(rb)
                except (RuntimeError, TypeError):
                    pass
        self._bands = {}

    def canvas(self):
        return self.ctx.iface.mapCanvas()

    def zoom_to_area(self) -> None:
        crs_util.zoom_to_rect(self.canvas(), self.area_geom.boundingBox(), self.crs, 1.15)

    def _hook_canvas(self) -> None:
        try:
            self.canvas().destinationCrsChanged.connect(self._on_canvas_crs)
            self._canvas_hooked = True
        except (RuntimeError, AttributeError):
            pass

    def _unhook_canvas(self) -> None:
        if self._canvas_hooked:
            try:
                self.canvas().destinationCrsChanged.disconnect(self._on_canvas_crs)
            except (RuntimeError, TypeError):
                pass
            self._canvas_hooked = False

    def _on_canvas_crs(self) -> None:
        if not self.closed:
            self._refresh_bands()

    def refresh_export_defaults(self) -> None:
        """Called when RV-Export opens: a fresh timestamp name and the folder for where the project is now."""
        if not self.export_name_edited:
            self.export_name = default_layer_name()
        if not self.export_folder_edited:
            self.export_folder = str(export_mod.default_folder(QgsProject.instance().fileName()))

    def resolve_target(self, name: Optional[str] = None, fmt: Optional[str] = None,
                       folder: Optional[str] = None):
        """``(name, path)`` the export would use: the typed name, or a numbered one when a file in the folder or a
        layer in the project already has it.  Nothing is ever replaced."""
        name = (name or self.export_name or default_layer_name()).strip() or default_layer_name()
        fmt = fmt or self.export_format
        folder = folder or self.export_folder
        taken = [l.name() for l in QgsProject.instance().mapLayers().values()]
        return export_mod.unique_target(folder, name, fmt, taken)

    def output_path(self, name: str, fmt: str) -> Path:
        return self.resolve_target(name, fmt)[1]

    def export(self, name: Optional[str] = None, fmt: Optional[str] = None,
               add_to_map: Optional[bool] = None, folder: Optional[str] = None) -> Dict[str, Any]:
        """Write the lines that are shown.

        Returns {path, folder, name, layer_name, format, feature_count, lines, length_km, by_class_km, layer,
        layer_id, layer_added, add_to_map}.  ``name`` is the final layer name (suffixed when the typed one was
        taken).  "Add to map" adds an OGR layer with ``QgsProject.addMapLayer`` directly: nothing here goes
        through the bridge registry, so it works whether or not the handlers were ever loaded."""
        fmt = fmt or self.export_format
        add = self.export_add_to_map if add_to_map is None else add_to_map
        rows, by = [], {c: 0.0 for c in range(1, 8)}
        for l in self.visible_lines():
            pts = l["geom"].asPolyline()
            if len(pts) < 2 or l["length_m"] < MIN_LINE_M:
                continue
            cid = int(l["class_id"])
            by[cid] = by.get(cid, 0.0) + l["length_m"] / 1000.0
            rows.append({"coords": [(p.x(), p.y()) for p in pts], "class_id": cid,
                         "class_name": theme.CLASS_NAMES.get(cid, f"class_{cid}"), "length_m": l["length_m"]})
        try:
            final_name, path = self.resolve_target(name, fmt, folder)
        except Exception as e:
            raise export_mod.explain(e, Path(folder or self.export_folder) / f"x{export_mod.extension(fmt)}") from e
        res = export_mod.write_lines(path, fmt, rows, self.crs.toWkt(), final_name)
        uri = f"{res['path']}|layername={res['layer_name']}" if fmt == "GeoPackage" else res["path"]
        res["style_saved"] = self._store_default_style(uri, fmt)
        layer = None
        if add:
            layer = QgsVectorLayer(uri, final_name, "ogr")
            if layer.isValid():
                self._style_lines(layer, variables=False)
                prj = QgsProject.instance()
                prj.addMapLayer(layer, False)
                prj.layerTreeRoot().insertLayer(0, layer)
            else:
                layer = None
        res.update(lines=len(rows), length_km=sum(by.values()), by_class_km=by, layer=layer,
                   layer_id=layer.id() if layer else "", layer_added=layer is not None, name=final_name,
                   folder=str(path.parent), add_to_map=add)
        return res

    def _store_default_style(self, uri: str, fmt: str) -> bool:
        """The exported file carries the line palette as its default style, so it looks the same when it is
        opened later or elsewhere: inside a GeoPackage (its layer_styles table), as a .qml beside a GeoJSON or a
        Shapefile.  Never fails the export: the lines are written by then, and taking them back after QGIS opened the
        file could itself fail on Windows and leave a partial file.  A failure goes to the message log only (QGIS's
        own text is never shown); an empty .qml it left is removed.  True when the style was saved."""
        qml = export_mod.style_file(uri.split("|", 1)[0])
        why = ""
        try:
            lyr = QgsVectorLayer(uri, "style", "ogr")
            if not lyr.isValid():
                why = "the exported file could not be opened again"
            else:
                self._style_lines(lyr, variables=False)
                cat = QgsMapLayer.StyleCategory.AllStyleCategories
                if fmt == "GeoPackage":
                    save = getattr(lyr, "saveStyleToDatabaseV2", None) or lyr.saveStyleToDatabase
                    r = save("Road Extractor", "Road Extractor line palette", True, "", cat)
                    why = (r[-1] if isinstance(r, tuple) else r) or ""
                else:
                    msg, ok = lyr.saveDefaultStyle(cat)
                    why = "" if ok else (msg or "not saved")
            del lyr
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
        if why:
            _log(f"Export: the line style was not saved with {uri}: {why}")
            try:
                if qml is not None and qml.exists() and qml.stat().st_size == 0:
                    qml.unlink()
            except OSError:
                pass
        return not why

    def activate_tool(self) -> None:
        """Make the review tool (hover, click, keys, pan) the canvas tool."""
        from .review_tool import ReviewTool

        cv = self.canvas()
        if self.tool is None:
            self.tool = ReviewTool(cv, self)
        cur = cv.mapTool()
        if cur is not self.tool and not getattr(cur, "_trb", False):
            self.prev_tool = cur
        self.keys_wanted = True
        cv.setMapTool(self.tool)

    def rearm(self, force: bool = False) -> bool:
        """Put the review tool back on the canvas after another QGIS map tool (Pan, Identify, Zoom In) took over.
        Called when the pointer or focus comes back to the panel, on window activation and by "Select lines".
        True when the tool was put back."""
        if self.closed or self.step != "keep" or not (self.keys_wanted or force) or self.tool_active():
            return False
        self.activate_tool()
        return True

    def release_tool(self) -> None:
        cv = self.canvas()
        self.keys_wanted = False
        self._hover = None
        self._refresh_bands()
        if self.tool is not None:
            self.tool.release()
        if self.tool is not None and cv.mapTool() is self.tool:
            cv.unsetMapTool(self.tool)
            if self.prev_tool is not None:
                try:
                    cv.setMapTool(self.prev_tool)
                except RuntimeError:
                    pass

    def tool_active(self) -> bool:
        return self.tool is not None and self.canvas().mapTool() is self.tool

    def has_lines(self) -> bool:
        return bool(self.lines)

    def close(self) -> None:
        """Remove every review layer, band and hook, and restore the map tool.  Raw lines are not kept."""
        if self.closed:
            return
        self.closed = True
        self.keys_wanted = False
        self._deb.stop()
        try:
            self.closing.emit()
        except RuntimeError:
            pass
        self._unhook_canvas()
        self._clear_bands()
        self.handler = None
        tool, self.tool = self.tool, None
        if tool is not None:
            try:
                cv = self.canvas()
                if cv.mapTool() is tool:
                    cv.unsetMapTool(tool)
                    if self.prev_tool is not None and not getattr(self.prev_tool, "_trb", False):
                        try:
                            cv.setMapTool(self.prev_tool)
                        except RuntimeError:
                            pass
                tool.release()
                from qgis.PyQt import sip
                sip.delete(tool)
            except RuntimeError:
                pass
        self.prev_tool = None
        self._unwatch_removal()
        prj = QgsProject.instance()
        try:
            self.layer.rollBack()
        except Exception:
            pass
        root = prj.layerTreeRoot()
        if self.group is not None:
            try:
                root.removeChildNode(self.group)
            except Exception:
                pass
        prj.removeMapLayers([i for i in self._layer_ids if prj.mapLayer(i) is not None])
        self.layers.clear()
