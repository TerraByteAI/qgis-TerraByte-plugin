"""Map-canvas pieces of the extract flow: the rectangle-drawing tool, the area preview and the run overlay.

Everything draws with standard ``QgsRubberBand`` items (canvas CRS, so they follow pan and zoom) and small QLabel
HUDs parented to the canvas.  Every item is transformed to the map's destination CRS through ``crs_util``, and every
signal, message-bar hook and canvas item is let go in ``clear()``.  The tool is driven by plain methods (``start``,
``drag_to``, ``release``, ``use_whole_image``, ``cancel``), so it can be driven without synthetic mouse events.

Colours: the area rectangle, the tile grid and the HUDs are the interface's light purple, each over a dark casing so
they read on bright imagery.  Extracted lines use the line palette of ``theme`` (one casing band and one colour band
per class).
"""
from __future__ import annotations

import weakref
from typing import Callable, Dict, List, Optional

from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsRectangle
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QEvent, QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox, QLabel, QLineEdit, QPlainTextEdit,
                                 QTextEdit, QWidget)

from ...cloud.grid import (MAX_TILES_HARD, AreaChoice, ImageryInfo, area_tiles_label, compute_grid, km2_label,
                           tile_cost_label, whole_image_geometry)
from .. import theme
from ..widgets import message_bar_inset, track
from . import crs_util
from .overlay_hooks import MessageBarHook, SignalGroup

POLY = Qgis.GeometryType.Polygon
LINE = Qgis.GeometryType.Line
MAX_DRAWN_TILES = MAX_TILES_HARD
OUTSIDE_TEXT = "That rectangle is outside the imagery."
EDGE_PX, EDGE_CASE_PX = 3, 7
GRID_PX, GRID_CASE_PX = 1, 3
UI_CASING = QColor(16, 17, 20, 225)
GRID_CASING = QColor(16, 17, 20, 110)


def outside_text(name: str = "") -> str:
    """The 'outside the imagery' message, naming the raster so a map showing other imagery is not a mystery."""
    return f"That rectangle is outside {name}." if name else OUTSIDE_TEXT


def _rgba(hexcol: str, a: float) -> QColor:
    c = QColor(hexcol)
    c.setAlphaF(a)
    return c


def _band(canvas, kind, fill: Optional[QColor], stroke: Optional[QColor], width: float = 1.0, dash=None):
    rb = QgsRubberBand(canvas, kind)
    rb.setFillColor(fill if fill is not None else QColor(0, 0, 0, 0))
    rb.setStrokeColor(stroke if stroke is not None else QColor(0, 0, 0, 0))
    rb.setWidth(width)
    if dash is not None:
        rb.setLineStyle(dash)
    return rb


class Hud(QLabel):
    """Pill / label drawn over the canvas (mono caps, translucent dark)."""

    def __init__(self, canvas: QgsMapCanvas, name: str, pill: bool = True, dot: bool = False):
        super().__init__(canvas)
        self.setObjectName(name)
        self.canvas = canvas
        self._pill = pill
        self._dot = dot
        self.setStyleSheet(
            f"QLabel{{background: rgba(7,8,14,0.82); color:{theme.TEXT}; border:1px solid {theme.BORDER_STRONG};"
            f"border-radius:{15 if pill else 6}px;"
            f"padding:{('7px 14px 7px ' + ('28px' if dot else '14px')) if pill else '4px 8px'};"
            f"font-family:{theme.FONT_MONO}; font-size:{11 if pill else 10}px;}}")
        track(self, 0.88 if pill else 0.8)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._dot:
            from qgis.PyQt.QtGui import QPainter
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.VIOLET))
            p.drawEllipse(15, self.height() // 2 - 3, 6, 6)

    def set_text(self, text: str, danger: bool = False) -> None:
        self.setText(text.upper())
        if danger:
            self.setStyleSheet(self.styleSheet().replace(f"color:{theme.TEXT};", f"color:{theme.DANGER};"))
        else:
            self.setStyleSheet(self.styleSheet().replace(f"color:{theme.DANGER};", f"color:{theme.TEXT};"))
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

    def reposition(self) -> None:
        if self._pill:
            self.move(max(0, (self.canvas.width() - self.width()) // 2), 16 + message_bar_inset(self.canvas))


def _n_tiles(n: int) -> str:
    """'1 tile', '36 tiles'."""
    return tile_cost_label(n)


def drawn_km2(area: AreaChoice) -> float:
    """The drawn area on the imagery in km² (the tiles' area when the area carries no drawn size)."""
    return area.drawn_km2 if getattr(area, "drawn_km2", 0) else area.area_km2


def area_text(area: AreaChoice) -> str:
    """The map label of a drawn area: '1.9 km² · 36 tiles' (the drawn area, then the tiles it uses; 'up to 49 tiles'
    while some may hold no imagery)."""
    return f"{km2_label(drawn_km2(area))} · {area_tiles_label(area)}"


class AreaPreview:
    """The drawn rectangle, its tile grid and the 'area · tiles' label, kept on the map until redrawn or the run
    starts.  The outline is a 3 px accent line over a 7 px dark casing and the grid a 1 px accent line over a 3 px dark
    casing, so both show on bright imagery while staying light purple."""

    def __init__(self, canvas: QgsMapCanvas):
        self.canvas = canvas
        self.run = None
        self.area: Optional[AreaChoice] = None
        self.geom: Optional[QgsGeometry] = None
        self._sig = SignalGroup()
        self.rb_tiles_case = _band(canvas, POLY, None, GRID_CASING, GRID_CASE_PX)
        self.rb_tiles = _band(canvas, POLY, _rgba(theme.ACCENT, 0.06), _rgba(theme.ACCENT, 0.8), GRID_PX)
        self.rb_case = _band(canvas, POLY, None, UI_CASING, EDGE_CASE_PX)
        self.rb_edge = _band(canvas, POLY, _rgba(theme.ACCENT, 0.10), QColor(theme.ACCENT), EDGE_PX)
        self.label = Hud(canvas, "drawAreaLabel", pill=False)
        self.pill = Hud(canvas, "drawHud")
        self._sig.connect(canvas.destinationCrsChanged, self.redraw)
        self._sig.connect(canvas.extentsChanged, self._place)
        self._bar = MessageBarHook(self._place)
        self._dead = False

    def set_area(self, area: Optional[AreaChoice], geom: Optional[QgsGeometry] = None) -> None:
        self.area, self.geom = area, geom
        self.redraw()

    def _bands(self) -> list:
        return [self.rb_tiles_case, self.rb_tiles, self.rb_case, self.rb_edge]

    def redraw(self, *_a) -> None:
        if self._dead:
            return
        for rb in self._bands():
            rb.reset(POLY)
        a = self.area
        if a is None:
            self.label.hide()
            return
        cv = self.canvas
        outline = QgsGeometry.fromWkt(a.geometry_wkt) if (a.tile_count and a.geometry_wkt) else self.geom
        if outline is not None and not outline.isNull() and not outline.isEmpty():
            g = crs_util.to_canvas_geometry(cv, outline, a.crs)
            self.rb_case.setToGeometry(g, None)
            self.rb_edge.setToGeometry(g, None)
        if a.tiles and len(a.tiles) <= MAX_DRAWN_TILES:
            tr = crs_util.to_canvas_transform(cv, a.crs)
            rects = []
            for r, c in a.tiles:
                tg = QgsGeometry.fromRect(QgsRectangle(*a.tile_rect(r, c)))
                if tr is not None:
                    tg.transform(tr)
                rects.append(tg)
            grid = QgsGeometry.collectGeometry(rects)
            self.rb_tiles_case.setToGeometry(grid, None)
            self.rb_tiles.setToGeometry(grid, None)
        self.label.set_text(area_text(a))
        self._place()

    def set_hint(self, text: str, danger: bool = False) -> None:
        if text:
            self.pill.set_text(text, danger=danger)
        else:
            self.pill.hide()

    def _place(self, *_a) -> None:
        if self._dead:
            return
        try:
            self.label.move(20, self.canvas.height() - 40)
            if not self.pill.isHidden():
                self.pill.reposition()
        except RuntimeError:
            pass

    def visible_label(self) -> str:
        return self.label.text() if not self.label.isHidden() else ""

    def clear(self) -> None:
        if self._dead:
            return
        self._dead = True
        self._sig.disconnect_all()
        self._bar.dispose()
        for rb in self._bands():
            try:
                rb.reset(POLY)
                self.canvas.scene().removeItem(rb)
            except RuntimeError:
                pass
        for h in (self.label, self.pill):
            try:
                h.hide()
                h.deleteLater()
            except RuntimeError:
                pass


_MOD = Qt.KeyboardModifier
DRAW_KEYS = (Qt.Key.Key_Backspace, Qt.Key.Key_Delete)


def draw_key(key, modifiers) -> bool:
    """Backspace or Delete without Ctrl, Alt or Meta: the keys QGIS answers with "Delete selected"."""
    return key in DRAW_KEYS and not (modifiers & (_MOD.ControlModifier | _MOD.AltModifier | _MOD.MetaModifier))


def _text_entry(w) -> bool:
    return isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)) or \
        (isinstance(w, QComboBox) and w.isEditable())


class _DrawKeys(QObject):
    """While the rectangle tool is the map tool, Backspace and Delete aimed at the map or at the panel are
    taken here (the ShortcutOverride, so QGIS's own shortcuts never fire, and the key press, so the canvas never
    passes it on to QGIS).  The same mechanism as the review tool: a filter on the map and on the focus widget when it
    is in the panel, never on the application; a text box keeps its own keys."""

    PROP = "_trb_draw_keys"

    def __init__(self, tool: "DrawAreaTool"):
        super().__init__()
        self._tool = weakref.ref(tool)
        self._targets: list = []
        self._hooked = False
        self.taken = 0

    def _ours(self, obj) -> bool:
        t = self._tool()
        if t is None or not isinstance(obj, QWidget) or _text_entry(obj):
            return False
        try:
            cv = t.canvas_
            if obj is cv or cv.isAncestorOf(obj):
                return True
            panel = t.panel
            return panel is not None and (obj is panel or panel.isAncestorOf(obj))
        except RuntimeError:
            return False

    def eventFilter(self, obj, ev):
        typ = ev.type()
        if typ not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            return False
        try:
            if not draw_key(ev.key(), ev.modifiers()) or not self._ours(obj):
                return False
            ev.accept()
            if typ == QEvent.Type.KeyPress:
                self.taken += 1
            return True
        except RuntimeError:
            return False

    def watch(self, w) -> None:
        if not isinstance(w, QWidget):
            return
        try:
            if w.property(self.PROP) or not self._ours(w):
                return
            w.installEventFilter(self)
            w.setProperty(self.PROP, True)
            self._targets.append(weakref.ref(w))
        except RuntimeError:
            pass

    def _focus_changed(self, _old, new) -> None:
        self.watch(new)

    def attach(self) -> None:
        t = self._tool()
        if t is None:
            return
        if not self._hooked:
            app = QApplication.instance()
            if app is not None:
                app.focusChanged.connect(self._focus_changed)
                self._hooked = True
        self.watch(t.canvas_)
        self.watch(QApplication.focusWidget())

    def detach(self) -> None:
        if self._hooked:
            try:
                QApplication.instance().focusChanged.disconnect(self._focus_changed)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self._hooked = False
        targets, self._targets = self._targets, []
        for ref in targets:
            w = ref()
            if w is None:
                continue
            try:
                w.removeEventFilter(self)
                w.setProperty(self.PROP, None)
            except RuntimeError:
                pass


class DrawAreaTool(QgsMapTool):
    """Press, drag, release draws a rectangle (``start`` / ``drag_to`` / ``release``); ``use_whole_image`` makes the
    raster's full extent the area through the same path.

    The rectangle, its tile grid and label live in ``self.preview`` and stay on the map after the release (and
    after the tool is switched off) until a new rectangle is drawn or the preview is cleared."""

    changed = pyqtSignal()
    drawn = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(self, canvas: QgsMapCanvas, info: ImageryInfo, tiles_left: Callable[[], Optional[int]],
                 panel: Optional[QWidget] = None):
        super().__init__(canvas)
        self.canvas_ = canvas
        self.info = info
        self.tiles_left = tiles_left
        self.panel = panel
        self.points: List[QgsPointXY] = []
        self.count = 0
        self.area: Optional[AreaChoice] = None
        self.anchor: Optional[QgsPointXY] = None
        self.whole = False
        self.preview = AreaPreview(canvas)
        self.keys = _DrawKeys(self)
        self._show_idle_hint()

    def geometry(self) -> Optional[QgsGeometry]:
        """The rectangle as a polygon in the IMAGERY CRS (corners transformed from the map CRS).  For the whole
        image it is the raster's own extent, in its own CRS: what a drag over the whole raster covers, exactly."""
        if len(self.points) != 4:
            return None
        if self.whole:
            return whole_image_geometry(self.info)
        g = QgsGeometry.fromPolygonXY([self.points + [self.points[0]]])
        return crs_util.from_canvas_geometry(self.canvas_, g, self.info.crs)

    def _show_idle_hint(self) -> None:
        self.preview.set_hint("Drag to draw a rectangle" if self.area is None else "Drag again to redraw")

    def start(self, pt: QgsPointXY) -> None:
        self.whole = False
        self.anchor = QgsPointXY(pt)
        self.drag_to(pt)

    def use_whole_image(self) -> bool:
        """'Use whole image': the area becomes the raster's full extent.  The corners are the raster's box on the map
        (so the preview and Redraw work as for a drag) and the geometry is the extent in the imagery CRS, fed to the
        same grid, preview and release as a dragged rectangle.  True when it covers some of the imagery."""
        r = crs_util.to_canvas_rect(self.canvas_, QgsRectangle(*self.info.extent), self.info.crs)
        if r.isNull() or r.isEmpty():
            return False
        self.anchor = None
        self.whole = True
        self.points = [QgsPointXY(r.xMinimum(), r.yMaximum()), QgsPointXY(r.xMaximum(), r.yMaximum()),
                       QgsPointXY(r.xMaximum(), r.yMinimum()), QgsPointXY(r.xMinimum(), r.yMinimum())]
        self.refresh()
        return self.release()

    def reset(self) -> None:
        """Forget the rectangle (Redraw): nothing on the map, ready for a new drag."""
        self.points, self.area, self.count, self.anchor, self.whole = [], None, 0, None, False
        self.preview.set_area(None)
        self.preview.set_hint("Drag to draw a rectangle")

    def drag_to(self, pt: QgsPointXY) -> None:
        a = self.anchor
        if a is None:
            return
        self.points = [QgsPointXY(a.x(), a.y()), QgsPointXY(pt.x(), a.y()), QgsPointXY(pt.x(), pt.y()),
                       QgsPointXY(a.x(), pt.y())]
        self.refresh()

    def refresh(self) -> None:
        g = self.geometry() if self.can_finish() else None
        area = compute_grid(self.info, g, source="raster" if self.whole else "drawn") if g is not None else None
        self.area = area
        self.count = area.tile_count if area else 0
        self.preview.set_area(area, g)
        if area is not None and area.tile_count == 0:
            self.preview.set_hint(outside_text(self.info.name), danger=True)
        else:
            self.preview.set_hint("Drag again to redraw" if self.area is not None else "Drag to draw a rectangle")
        self.changed.emit()

    def show_area(self, area: AreaChoice) -> None:
        """Show a rectangle drawn earlier (back from the run check)."""
        self.area, self.count = area, area.tile_count
        self.whole = bool(getattr(area, "whole_image", False))
        self.preview.set_area(area, QgsGeometry.fromWkt(area.geometry_wkt))
        self.preview.set_hint("Drag again to redraw")
        self.changed.emit()

    def over_budget(self) -> bool:
        """More tiles than the balance.  An upper bound (some tiles may hold no imagery) never is: the run is judged on
        the exact count."""
        left = self.tiles_left()
        return left is not None and self.count > left and not getattr(self.area, "upper_bound", False)

    def can_finish(self) -> bool:
        return (len(self.points) == 4 and self.points[0].x() != self.points[2].x()
                and self.points[0].y() != self.points[2].y())

    def release(self) -> bool:
        """The mouse button went up: the rectangle is final.  True when it covers some of the imagery."""
        self.anchor = None
        if not self.can_finish() or self.area is None or self.area.tile_count == 0:
            return False
        g = self.geometry()
        self.drawn.emit(g)
        return True

    def abort_drag(self) -> None:
        """Esc during a drag: forget the rectangle being drawn (a later release does nothing)."""
        self.reset()
        self.changed.emit()

    def cancel(self) -> None:
        self.cancelled.emit()

    def clear(self) -> None:
        """Forget the rectangle and take every canvas item and hook away (the tool is finished with)."""
        self.anchor = None
        self.points = []
        self.area = None
        self.count = 0
        self.whole = False
        self.keys.detach()
        self.preview.clear()

    def detach_preview(self) -> AreaPreview:
        """Hand the preview over (it stays on the map); the tool no longer owns it."""
        pv = self.preview
        pv.set_hint("")
        return pv

    def canvasMoveEvent(self, e):
        if self.anchor is not None:
            self.drag_to(e.mapPoint())

    def canvasPressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.points = []
            self.area = None
            self.start(e.mapPoint())

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.anchor is not None:
            self.release()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            if self.anchor is not None:
                self.abort_drag()
            else:
                self.cancel()
            e.accept()
        elif draw_key(e.key(), e.modifiers()):
            e.ignore()

    def activate(self):
        super().activate()
        self.keys.attach()

    def deactivate(self):
        """Another map tool took over (pan, identify...).  The rectangle stays; the panel re-arms this tool."""
        self.anchor = None
        self.keys.detach()
        super().deactivate()


LINE_Z = 10.0


def line_bands(canvas: QgsMapCanvas, class_id: int):
    """(casing, colour) rubber bands of one line class, from the palette in ``theme``: the dark casing at
    ``casing_width_px`` under the class colour at ``line_width_px``.  Higher classes in the palette's order are drawn
    under lower ones (Highway on top)."""
    cid = int(class_id)
    case = _band(canvas, LINE, None, QColor(theme.LINE_CASING), theme.casing_width_px(cid))
    line = _band(canvas, LINE, None, QColor(theme.class_colour(cid)), theme.line_width_px(cid))
    rank = 0.01 * (len(theme.LINE_PALETTE) + 1 - cid)
    case.setZValue(LINE_Z + rank)
    line.setZValue(LINE_Z + 1.0 + rank)
    return case, line


class RunOverlay:
    """Tile states and finished lines of a run on the canvas (EX-Running).  The grid, the area rectangle and the
    pill are the interface's light purple; the lines take the line palette, one casing band and one colour band per
    class (``rb_casings`` / ``rb_lines``, keyed by class id)."""

    def __init__(self, canvas: QgsMapCanvas, run, labels: bool = True):
        self.canvas = canvas
        self.run = run
        self.area: AreaChoice = run.area
        self._dead = False
        self._sig = SignalGroup()
        self.rb_grid = _band(canvas, POLY, None, _rgba(theme.ACCENT, 0.35), 1)
        self.rb_extent_case = _band(canvas, POLY, None, UI_CASING, EDGE_CASE_PX - 1)
        self.rb_extent = _band(canvas, POLY, _rgba(theme.ACCENT, 0.08), QColor(theme.ACCENT), EDGE_PX - 1)
        self.rb_queued = _band(canvas, POLY, _rgba("#07080e", 0.55), None, 0)
        self.rb_decoding = _band(canvas, POLY, _rgba(theme.ACCENT, 0.14), QColor(theme.ACCENT), 2)
        self.rb_lines: Dict[int, QgsRubberBand] = {}
        self.rb_casings: Dict[int, QgsRubberBand] = {}
        self.pill = Hud(canvas, "runPill", dot=True)
        self.pill.set_text("Lines appear as each tile finishes")
        self.label = Hud(canvas, "runLabel", pill=False)
        self.label.set_text(area_text_of_run(self.area, run))
        self._place()
        self._tr = self._transform()
        self.counts: Dict[str, int] = {}
        self._clip = self._area_clip()
        self._draw_static()
        self.update()
        self._sig.connect(run.changed, self.update)
        self._sig.connect(canvas.destinationCrsChanged, self._crs_changed)
        self._sig.connect(canvas.extentsChanged, self._place)
        self._bar = MessageBarHook(self._place)

    def _place(self, *_a) -> None:
        if self._dead:
            return
        try:
            self.label.move(20, self.canvas.height() - 40)
            if not self.pill.isHidden():
                self.pill.reposition()
        except RuntimeError:
            pass

    def _transform(self):
        return crs_util.to_canvas_transform(self.canvas, self.area.crs)

    def _crs_changed(self, *_a) -> None:
        """The project CRS changed mid-run: redo every shape in the new CRS."""
        if self._dead:
            return
        self._tr = self._transform()
        self._draw_static()
        self.update()

    def _rect(self, r, c) -> QgsGeometry:
        g = QgsGeometry.fromRect(QgsRectangle(*self.area.tile_rect(r, c)))
        if self._tr is not None:
            g.transform(self._tr)
        return g

    def _area_clip(self) -> Optional[QgsGeometry]:
        """The drawn area (in the imagery CRS) widened by one pixel: live lines are clipped to it, so nothing shows in
        the padding that edge tiles carry past the raster edge."""
        wkt = getattr(self.area, "geometry_wkt", "") or ""
        g = QgsGeometry.fromWkt(wkt) if wkt else QgsGeometry()
        if g.isNull() or g.isEmpty():
            return None
        return g.buffer(abs(self.area.pixel_size), 4)

    def _draw_static(self) -> None:
        self._static_tiles = len(self.area.tiles)
        geoms = [self._rect(r, c) for r, c in self.area.tiles]
        self.rb_grid.reset(POLY)
        if not geoms:
            return
        self.rb_grid.setToGeometry(QgsGeometry.collectGeometry(geoms), None)
        ext = QgsGeometry.unaryUnion(geoms)
        wkt = getattr(self.area, "geometry_wkt", "") or ""
        area_g = QgsGeometry.fromWkt(wkt) if wkt else QgsGeometry()
        if not area_g.isNull() and not area_g.isEmpty():
            if self._tr is not None:
                area_g.transform(self._tr)
            ext = area_g
        self.rb_extent_case.setToGeometry(ext, None)
        self.rb_extent.setToGeometry(ext, None)

    def _clipped_parts(self, pixel_pts) -> List[List[QgsPointXY]]:
        """One live line (run pixels) -> its pieces inside the drawn area, in canvas CRS."""
        rr = self.run.run_ref()
        mp = [QgsPointXY(*rr.pixel_to_map(x, y)) for x, y in pixel_pts]
        if len(mp) < 2:
            return []
        parts = [mp]
        if self._clip is not None:
            g = QgsGeometry.fromPolylineXY(mp)
            if not self._clip.contains(g):
                inter = g.intersection(self._clip)
                parts = []
                if not inter.isNull() and not inter.isEmpty():
                    for sub in inter.asGeometryCollection():
                        if sub.type() != Qgis.GeometryType.Line:
                            continue
                        parts.extend(sub.asMultiPolyline() if sub.isMultipart() else [sub.asPolyline()])
        out = []
        for part in parts:
            if len(part) >= 2:
                out.append([QgsPointXY(self._tr.transform(p)) if self._tr is not None else p for p in part])
        return out

    def update(self) -> None:
        if self._dead:
            return
        run = self.run
        if len(self.area.tiles) != getattr(self, "_static_tiles", -1):
            self._draw_static()
            self.label.set_text(area_text_of_run(self.area, run))
        done, started = run.done_tiles, run.started_tiles
        queued = [self._rect(r, c) for r, c in run.order if (r, c) not in done and (r, c) not in started]
        decoding = [self._rect(r, c) for (r, c) in started]
        for band, geoms in ((self.rb_queued, queued), (self.rb_decoding, decoding)):
            band.reset(POLY)
            if geoms:
                band.setToGeometry(QgsGeometry.collectGeometry(geoms), None)
        self.counts = {"finished": len(done), "decoding": len(started), "queued": len(queued)}
        by_class: Dict[int, List[QgsGeometry]] = {}
        for rc, lines in run.tile_lines.items():
            for ln in lines:
                try:
                    cid = int(ln["class_id"])
                except (KeyError, TypeError, ValueError):
                    cid = 0
                for pts in self._clipped_parts(ln["points"]):
                    by_class.setdefault(cid, []).append(QgsGeometry.fromPolylineXY(pts))
        for cid in set(self.rb_lines) - set(by_class):
            self.rb_lines[cid].reset(LINE)
            self.rb_casings[cid].reset(LINE)
        for cid, geoms in by_class.items():
            if cid not in self.rb_lines:
                self.rb_casings[cid], self.rb_lines[cid] = line_bands(self.canvas, cid)
            g = QgsGeometry.collectGeometry(geoms)
            for rb in (self.rb_casings[cid], self.rb_lines[cid]):
                rb.reset(LINE)
                rb.setToGeometry(g, None)

    def clear(self) -> None:
        if self._dead:
            return
        self._dead = True
        self._sig.disconnect_all()
        self._bar.dispose()
        for rb in [self.rb_grid, self.rb_extent_case, self.rb_extent, self.rb_queued, self.rb_decoding,
                   *self.rb_casings.values(), *self.rb_lines.values()]:
            try:
                rb.reset(POLY)
                self.canvas.scene().removeItem(rb)
            except RuntimeError:
                pass
        self.rb_lines.clear()
        self.rb_casings.clear()
        for h in (self.pill, self.label):
            try:
                h.hide()
                h.deleteLater()
            except RuntimeError:
                pass


def area_text_of_run(area: AreaChoice, run) -> str:
    """The run's map label: the drawn area and the tiles it uses, as on Draw ('1.9 km² · 36 tiles')."""
    return f"{km2_label(drawn_km2(area))} · {area_tiles_label(area)}"
