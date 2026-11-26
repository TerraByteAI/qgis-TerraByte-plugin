"""The Extract flow: picking the imagery, drawing the area, the run check, the run and how it ended.

Screens: EX-NoImagery, EX-Start, EX-DrawArea, EX-Setup (the run check), EX-OverBudget, EX-Running, EX-Failed.
Run state lives in ``ctx.run`` (``cloud/runner.py``), the chosen imagery in ``ctx.imagery``
(``cloud/grid.ImageryInfo``) and the area in ``ctx.area`` (``cloud/grid.AreaChoice``);
``ctx.extras['classes_kept']`` holds the kept class ids.
"""
from __future__ import annotations

import os
import sys
import time
import weakref
from typing import List, Optional

from qgis.core import QgsGeometry, QgsProject, QgsRasterLayer, Qgis
from qgis.PyQt.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                                 QStyledItemDelegate, QVBoxLayout, QWidget)

from ...cloud import estimate
from ...cloud.grid import gsd_label, inspect_layer, side_label, compute_grid, tile_cost_label
from ...cloud.grid import RUN_TILE_LIMIT, area_tiles_label, km2_label, res_label
from ...core.numbers import count
from ...cloud.runner import RunController, stream_spec
from .. import icons, theme
from ..widgets import (FitScrollArea, SCROLLBAR_QSS, style_combo_popup, card_dialog, track, ArrowButton, ElideLabel, Meter, PillButton, Screen, SectionHeading, Swatch, button, card, hairline, hbox, label,
                       name_combo, pill_button, set_tip, vbox)
from . import crs_util, registry
from . import resolution as res
from .overlay_hooks import SignalGroup
from .controller import log
from .map_tools import OUTSIDE_TEXT, outside_text

SUPPORT_EMAIL = "info@terrabyte.ai"
QUOTA_MAILTO = f"mailto:{SUPPORT_EMAIL}?subject=Road%20Extractor%20quota%20increase"
OVER_BUDGET_POLL_MS = 20000
HELP_MAILTO = f"mailto:{SUPPORT_EMAIL}"
ALL_CLASSES = [c[0] for c in theme.CLASSES]


def project_rasters() -> List[QgsRasterLayer]:
    """All raster layers, file rasters first (the picker lists them first)."""
    ls = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsRasterLayer)]
    return sorted(ls, key=lambda l: (l.providerType() != "gdal", l.name().lower()))


def file_rasters() -> List[QgsRasterLayer]:
    return [l for l in project_rasters() if l.providerType() == "gdal" and l.isValid()]


def selected_layer(ctx) -> Optional[QgsRasterLayer]:
    lid = ctx.extras.get("layer_id")
    lyr = QgsProject.instance().mapLayer(lid) if lid else None
    if isinstance(lyr, QgsRasterLayer):
        return lyr
    files = file_rasters()
    if files:
        active = None
        try:
            active = ctx.iface.activeLayer()
        except Exception:
            pass
        if isinstance(active, QgsRasterLayer) and active in files:
            return active
        return files[0]
    return None


def imagery_in_view(canvas, info) -> bool:
    """True when the chosen raster's extent overlaps the map's view (both compared in the map's CRS)."""
    from qgis.core import QgsRectangle

    r = crs_util.to_canvas_rect(canvas, QgsRectangle(*info.extent), info.crs)
    return not r.isNull() and canvas.extent().intersects(r)


def zoom_to_imagery(ctx, info=None, only_if_out_of_view: bool = False) -> bool:
    """Fit the map to the raster picked in the panel.  True when the view changed."""
    from qgis.core import QgsRectangle

    info = info or ctx.imagery
    if info is None or not any(info.extent):
        return False
    canvas = ctx.iface.mapCanvas()
    if only_if_out_of_view and imagery_in_view(canvas, info):
        return False
    crs_util.zoom_to_rect(canvas, QgsRectangle(*info.extent), info.crs)
    return True


def imagery_present(run) -> Optional[QgsRasterLayer]:
    """The project layer the run used (same file or same name), or None if it was removed."""
    for l in file_rasters():
        if l.source().split("|")[0] == run.imagery.source or l.name() == run.imagery.name:
            return l
    return None


def class_swatch_name(cid: int) -> str:
    return theme.CLASS_NAMES[cid]


def route_start(ctx, **_kw) -> str:
    """EX-Start alias: EX-NoImagery when no file raster is in the project, else EX-Start (a raster that can't
    run leaves ``ctx.imagery`` empty and its reason in ``ctx.extras['imagery_check']``)."""
    if not file_rasters():
        ctx.imagery = None
        return "EX-NoImagery"
    lyr = selected_layer(ctx)
    if lyr is None:
        ctx.imagery = None
        return "EX-NoImagery"
    ctx.extras["layer_id"] = lyr.id()
    info = inspect_layer(lyr)
    ctx.extras["imagery_check"] = info
    ctx.imagery = info if info.usable else None
    return "EX-Start"


registry.alias("EX-Start")(route_start)

STALE_RASTER_TEXT = "The imagery is no longer in this project."
DELETED_HEADING = "This run's data was deleted"
DELETED_TEXT = "This run's lines were deleted after it ended. Run the area again to see them."
RUN_AGAIN_TEXT = "Run the area again"


def imagery_in_project(ctx) -> bool:
    """The raster the flow refers to is a file raster of the open project right now (never a remembered path)."""
    i = ctx.imagery
    if i is None:
        return False
    lyr = QgsProject.instance().mapLayer(getattr(i, "layer_id", None) or "")
    return isinstance(lyr, QgsRasterLayer) and lyr.providerType() == "gdal"


def run_gate(run, leave: bool = True) -> bool:
    """The gate: a run is created, started or retried only while its raster is a layer of the open
    project right now.  Every RunController entry point that reaches the service asks this first.  Refused: say so
    in the standard sentence and (when ``leave``) go to Start."""
    ctx = run.ctx
    lyr = imagery_present(run)
    if lyr is not None and lyr.providerType() == "gdal":
        info = inspect_layer(lyr)
        if info.usable:
            run.imagery = info
            return True
    if leave:
        run.dispose()
        if ctx.run is run:
            ctx.run = None
        ctx.extras.pop("layer_id", None)
        ctx.imagery = ctx.area = None
        refuse_stale_imagery(ctx)
    else:
        try:
            ctx.iface.messageBar().pushMessage("Road Extractor", STALE_RASTER_TEXT, Qgis.MessageLevel.Warning, 8)
        except Exception:
            pass
    return False


RunController.gate = staticmethod(run_gate)


def refuse_stale_imagery(ctx) -> None:
    """The raster is gone from the project: drop what refers to it, say so in a plain sentence and go to Start."""
    ctx.imagery = ctx.area = None
    ctx.extras.pop("layer_id", None)
    clear_overlay(ctx)
    try:
        ctx.iface.messageBar().pushMessage("Road Extractor", STALE_RASTER_TEXT, Qgis.MessageLevel.Warning, 8)
    except Exception:
        pass
    ref = weakref.ref(ctx)

    def to_start():
        c = ref()
        if c is None:
            return
        try:
            c.go("EX-Start")
        except Exception:
            pass
    QTimer.singleShot(0, to_start)


def _on_imagery(a, i) -> bool:
    """The rectangle's grid origin lies on this raster's pixel grid and inside its extent."""
    x0, y0, x1, y1 = i.extent
    return x0 - 1e-6 <= a.origin[0] <= x1 + 1e-6 and y0 - 1e-6 <= a.origin[1] <= y1 + 1e-6


@registry.alias("EX-Setup")
def route_setup(ctx, **_kw) -> str:
    a, left = ctx.area, ctx.tiles_left()
    i = ctx.imagery
    if a is not None and not imagery_in_project(ctx):
        ctx.imagery = ctx.area = None
        return "EX-Start"
    ri = res.run_imagery(ctx) if (a is not None and i is not None) else None
    if a is not None and i is not None and (ri is None or a.crs != i.crs
                                            or abs(a.pixel_size - ri.pixel_size) > 1e-9 * ri.pixel_size
                                            or abs(a.gsd_m - ri.gsd_m) > 1e-9 * ri.gsd_m
                                            or tuple(a.origin) and not _on_imagery(a, i)):
        ctx.area = None
        return "EX-DrawArea"
    upper = bool(getattr(a, "upper_bound", False))
    if a is not None and (over_run_limit(a.tile_count) or (not upper and left is not None and a.tile_count > left)):
        return "EX-OverBudget"
    return "EX-Setup"


def over_run_limit(tiles: int) -> bool:
    """More tiles than one run may use (4,096)."""
    return tiles > RUN_TILE_LIMIT


def add_raster(ctx) -> None:
    """Open QGIS's add-raster dialog (file chooser fallback outside QGIS)."""
    iface = ctx.iface
    act = getattr(iface, "actionAddRasterLayer", None)
    if callable(act):
        try:
            act().trigger()
            return
        except Exception as e:
            log(f"add raster action failed: {e}", Qgis.MessageLevel.Warning)
    path, _ = QFileDialog.getOpenFileName(iface.mainWindow() if hasattr(iface, "mainWindow") else None,
                                          "Add raster", "", "Rasters (*.tif *.tiff *.jpg *.jpeg *.png *.vrt);;All files (*)")
    if path:
        lyr = QgsRasterLayer(path, os.path.basename(path))
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)


def clear_overlay(ctx) -> None:
    ov = ctx.extras.pop("overlay", None)
    if ov is not None:
        ov.clear()
        try:
            ctx.iface.mapCanvas().refresh()
        except Exception:
            pass


def _connect_project(ctx) -> None:
    """Re-route the start family when layers come and go."""
    if ctx.extras.get("_proj_connected"):
        return
    ctx.extras["_proj_connected"] = True
    ref = weakref.ref(ctx)

    def changed(*_a):
        c = ref()
        if c is None:
            for sig in (p.layersAdded, p.layersRemoved):
                try:
                    sig.disconnect(changed)
                except (TypeError, RuntimeError):
                    pass
            return
        try:
            b = c.current_board
            if b in ("EX-NoImagery", "EX-Start"):
                QTimer.singleShot(0, lambda: _reroute(ref))
            elif b == "EX-Failed":
                s = c.panel.screen("EX-Failed")
                if s is not None:
                    QTimer.singleShot(0, s.refresh)
        except RuntimeError:
            pass

    p = QgsProject.instance()
    p.layersAdded.connect(changed)
    p.layersRemoved.connect(changed)


def _reroute(ref) -> None:
    c = ref()
    if c is None:
        return
    try:
        if c.current_board in ("EX-NoImagery", "EX-Start"):
            c.go("EX-Start")
    except RuntimeError:
        pass


def _svg(name: str, size: int, colour: str, stroke: Optional[float] = None, top: int = 0) -> QLabel:
    """An SVG icon as a fixed-size label (``top`` adds that many pixels of space above the icon)."""
    lb = QLabel()
    lb.setFixedSize(size, size + top)
    lb.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom if top else Qt.AlignmentFlag.AlignCenter)
    lb.setPixmap(icons.pixmap(name, size, colour, stroke))
    lb.setStyleSheet("background:transparent; border:none;")
    lb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    return lb


def _scroll_page(screen: QWidget) -> QVBoxLayout:
    """A scrolling main column (padding 14/14/16, gap 10) inside ``screen``; returns its layout."""
    screen.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    screen.setStyleSheet(f"QWidget#{screen.objectName()}{{background:{theme.BG_COVER};}}")
    outer = vbox(screen)
    sa = FitScrollArea()
    sa.setStyleSheet(SCROLLBAR_QSS)
    body = QWidget()
    body.setObjectName("pageBody")
    body.setStyleSheet("QWidget#pageBody{background:transparent;}")
    sa.setWidget(body)
    outer.addWidget(sa, 1)
    bar = QWidget()
    bar.setObjectName("pinnedBar")
    bar.setStyleSheet("QWidget#pinnedBar{background:transparent;}")
    bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    screen.pinned = vbox(bar, margins=(14, 6, 14, 16), spacing=8)
    screen.pinned_bar = bar
    outer.addWidget(bar, 0)
    return vbox(body, margins=(14, 14, 14, 10), spacing=10)


def _caption(text: str, name: str = "") -> QLabel:
    lb = label(text.upper(), mono=True, wrap=False, name=name)
    return lb


def _row_card(h: int = 38) -> QFrame:
    f = QFrame()
    f.setObjectName("rowCard")
    f.setStyleSheet(f"QFrame#rowCard{{background:rgba(247,245,251,0.03); border:1px solid {theme.HAIRLINE};"
                    f" border-radius:8px;}} QLabel{{border:none; background:transparent; color:{theme.TEXT_2};}}")
    f.setMinimumHeight(h)
    return f


def _resolution_row(name: str, on_change, rule_below: bool = False):
    """'Runs at 60 cm · image is 10 cm' with a Change link (EX-DrawArea, EX-Setup, EX-OverBudget);
    returns (row, label, Change button)."""
    r = QFrame()
    r.setObjectName(name + "Row")
    r.setFixedHeight(38)
    rule = "border-bottom:1px solid rgba(247,245,251,0.08);" if rule_below else ""
    r.setStyleSheet(f"QFrame#{name}Row{{border:none; {rule} background:transparent;}}")
    h = hbox(r, margins=(12, 0, 12, 0), spacing=10)
    h.addWidget(_svg("table-grid", 15, theme.TEXT_2))
    lb = ElideLabel("", name)
    lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    lb.setStyleSheet(f"color:{theme.TEXT_2};")
    h.addWidget(lb, 1)
    btn = button("Change", link=True, name=name + "Change")
    btn.setAccessibleName("Change resolution")
    set_tip(btn, res.CHANGE_TIP)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(f"QPushButton{{border:none; background:transparent; color:{theme.ACCENT}; font-size:12px;"
                      f" font-weight:500; padding:0 2px; text-decoration:none;}} QPushButton:hover{{color:{theme.TEXT};}}")
    btn.clicked.connect(on_change)
    h.addWidget(btn)
    return r, lb, btn


def change_resolution(ctx) -> None:
    """'Change' beside the resolution: back to the Resolution card on Start; the area is cleared."""
    ctx.area = None
    clear_overlay(ctx)
    ctx.go("EX-Start")


def _recommended_html(a60, link: bool = False) -> str:
    """'36 tiles at 60 cm (recommended)' with the count in the text colour (the comparison line on Draw, the run
    check and EX-OverBudget); ``link`` adds 'Change resolution and redraw'."""
    txt = f"<span style='color:{theme.TEXT}; font-weight:400'>{res.recommended_count(a60)}</span> (recommended)"
    if link:
        txt += (f". <a href='change' style='color:{theme.ACCENT}; font-weight:500; text-decoration:none'>"
                f"Change resolution and redraw</a>")
    return txt


class LockedSource(QWidget):
    """'Imagery and area · locked for this run' + read-only file row (with spec): Running and Failed."""

    def __init__(self, ctx, with_spec: bool = True):
        super().__init__()
        self.ctx = ctx
        lay = vbox(self, spacing=6)
        lay.addWidget(_caption("Imagery and area · locked for this run", "lockedCaption"))
        row = _row_card()
        h = hbox(row, margins=(12, 0, 12, 0), spacing=10)
        h.addWidget(_svg("image", 15, theme.TEXT_2))
        self.name = ElideLabel("", "lockedImagery")
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        h.addWidget(self.name, 1)
        self.spec = label("", mono=True, wrap=False, name="lockedSpec")
        self.spec.setStyleSheet(f"color:{theme.TEXT_2}; font-family:{theme.FONT_MONO}; font-size:10px;")
        h.addWidget(self.spec)
        self.spec.setVisible(with_spec)
        h.addWidget(_svg("padlock", 13, theme.TEXT_2))
        lay.addWidget(row)

    def refresh(self) -> None:
        run = self.ctx.run
        if run is None:
            return
        _put(self.name, run.imagery.name)
        _put(self.spec, f"{_drawn_km2_text(run.area)} · {res_label(run.area.gsd_m)}".upper())


def _drawn_km2_text(area) -> str:
    """'1.9 km²': the drawn area, as on the run check and the map label."""
    from .map_tools import drawn_km2

    return km2_label(drawn_km2(area))


def _how_it_works() -> QFrame:
    c = card(name="howItWorks")
    lay = vbox(c, margins=(14, 14, 14, 14), spacing=10)
    lay.addWidget(SectionHeading("How it works"))
    steps = [("Pick an area", "Draw a rectangle, or use the whole image."),
             ("Extract", "Seven line types, drawn on your map."),
             ("Keep and export", "Tick what to keep, then export.")]
    for i, (t, b) in enumerate(steps, 1):
        row = hbox(spacing=12)
        n = label(str(i), mono=True, wrap=False)
        n.setFixedSize(22, 22)
        n.setAlignment(Qt.AlignmentFlag.AlignCenter)
        n.setStyleSheet(f"border:1px solid rgba(200,173,255,0.45); border-radius:11px; color:{theme.ACCENT};"
                        f" font-family:{theme.FONT_MONO}; font-size:11px;")
        row.addWidget(n, 0, Qt.AlignmentFlag.AlignTop)
        col = vbox(spacing=1)
        tt = label(t, name=f"howStep{i}Title")
        tt.setStyleSheet("font-size:13px; font-weight:500;")
        col.addWidget(tt)
        bd = label(b, muted=True, name=f"howStep{i}Body")
        bd.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        col.addWidget(bd)
        row.addLayout(col, 1)
        lay.addLayout(row)
    return c


def _clean(text: str) -> str:
    """Service text for the screen: bracket placeholders (``[like this]``) never get through."""
    import re
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", text or "")).strip()


def _plain(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html)


@registry.screen("EX-NoImagery")
class ExNoImagery(Screen):
    def __init__(self, ctx):
        super().__init__(ctx)
        _connect_project(ctx)
        lay = _scroll_page(self)
        blk = vbox(spacing=10, margins=(16, 22, 16, 6))
        icon = label("", wrap=False, name="emptyIcon")
        icon.setFixedSize(56, 56)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(f"border:1px solid rgba(200,173,255,0.22); border-radius:28px;"
                           f" background:rgba(139,92,246,0.10);")
        icons.set_label_icon(icon, "image-off", 24, theme.ACCENT, 1.5)
        blk.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self.heading = label("No imagery in this project", h1=True, name="emptyHeading")
        self.heading.setStyleSheet("font-size:18px; font-weight:500;")
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        blk.addWidget(self.heading)
        self.body = label("Add a raster file to the project to start.", muted=True, name="emptyBody")
        self.body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        blk.addWidget(self.body)
        lay.addLayout(blk)
        c = card(name="imageryThatWorks")
        cl = vbox(c, margins=(14, 14, 14, 14), spacing=10)
        cl.addWidget(SectionHeading("Imagery that works"))
        bullets = ["A <b>GeoTIFF</b>, or a <b>JPG or PNG</b> that has a position on the map.",
                   "Colour imagery (red, green, blue).",
                   "<b>60 cm per pixel or sharper</b> works best."]
        self.bullets = []
        for i, t in enumerate(bullets, 1):
            r = hbox(spacing=9)
            r.addWidget(_svg("check", 14, theme.SUCCESS, 1.8, top=3), 0, Qt.AlignmentFlag.AlignTop)
            lb = label(t, name=f"workBullet{i}")
            lb.setStyleSheet("font-weight:400;")
            lb.setTextFormat(Qt.TextFormat.RichText)
            self.bullets.append(lb)
            r.addWidget(lb, 1)
            cl.addLayout(r)
        cl.addWidget(hairline())
        self.note = label("Make sure your licence lets you send this imagery to TerraByte.",
                          muted=True, name="basemapNote")
        self.note.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        nr = hbox(spacing=8)
        nr.addWidget(_svg("info-circle", 14, "#06b6d4", 1.6, top=2),
                     0, Qt.AlignmentFlag.AlignTop)
        nr.addWidget(self.note, 1)
        cl.addLayout(nr)
        lay.addWidget(c)
        lay.addStretch(1)
        self.add_btn = ArrowButton("Add raster…")
        self.add_btn.setObjectName("addRasterButton")
        self.add_btn.clicked.connect(lambda: add_raster(ctx))
        self.pinned.addWidget(self.add_btn)

    def on_show(self, **kw):
        _connect_project(self.ctx)


class _ComboOverlay(QObject):
    """Keeps the picker's image icon (left 12, top 12) and chevron (right 12, top 12) pinned over the combo."""

    def __init__(self, host: QWidget, left: QLabel, right: QLabel):
        super().__init__(host)
        self.host, self.left, self.right = host, left, right
        self._place()

    def _place(self) -> None:
        self.left.move(12, 12)
        self.right.move(max(0, self.host.width() - 12 - 16), 12)
        self.left.raise_()
        self.right.raise_()

    def eventFilter(self, o, ev):
        if ev.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._place()
        return False


class _ImageryPicker(Screen):
    """Imagery field + combo (EX-Start)."""

    def _build_picker(self, lay: QVBoxLayout) -> None:
        field = vbox(spacing=6)
        field.addWidget(_caption("Imagery", "imageryLabel"))
        self.combo = QComboBox()
        self.combo.setObjectName("imageryCombo")
        name_combo(self.combo, "Imagery", "Pick a raster layer from the project")
        self.combo.setFixedHeight(40)
        style_combo_popup(self.combo)
        self.combo.setStyleSheet(self._combo_qss(False))
        self.combo.activated.connect(self._picked)
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(self.combo)
        left = _svg("image", 16, theme.TEXT_2)
        right = _svg("chevron-down", 16, theme.TEXT_2, 1.8)
        for ov in (left, right):
            ov.setParent(wrap)
        wrap.installEventFilter(_ComboOverlay(wrap, left, right))
        field.addWidget(wrap)
        lay.addLayout(field)

    @staticmethod
    def _combo_qss(invalid: bool) -> str:
        border = "rgba(248,113,113,0.6)" if invalid else theme.BORDER_STRONG
        return (f"QComboBox{{padding:0 36px 0 38px; border-radius:8px; border:1px solid {border};"
                f" background:rgba(247,245,251,0.06); color:{theme.TEXT}; font-size:13px; font-weight:400;}}"
                "QComboBox::drop-down{border:none; width:0px;}"
                "QComboBox::down-arrow{image:none; width:0px; height:0px;}")

    def fill_combo(self) -> None:
        cur = self.ctx.extras.get("layer_id")
        self.combo.blockSignals(True)
        self.combo.clear()
        for l in file_rasters():
            self.combo.addItem(l.name(), l.id())
        i = self.combo.findData(cur)
        self.combo.setCurrentIndex(max(0, i))
        self.combo.blockSignals(False)

    def _picked(self, _idx: int) -> None:
        if self.ctx.extras.get("layer_id") != self.combo.currentData():
            self.ctx.area = None
        self.ctx.extras["layer_id"] = self.combo.currentData()
        self.ctx.go("EX-Start")
        zoom_to_imagery(self.ctx)

    def pick_layer(self, layer_id: str) -> None:
        """Pick the raster with id ``layer_id`` in code, as a choice in the combo does ('Pick a file raster')."""
        if self.ctx.extras.get("layer_id") != layer_id:
            self.ctx.area = None
        self.ctx.extras["layer_id"] = layer_id
        self.ctx.go("EX-Start")
        zoom_to_imagery(self.ctx)


FIELD_ACCENT = "rgba(200,173,255,0.6)"
RIGHT_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def _caption_row(caption: str, cap_name: str, right_name: str):
    """A field's mono caption with a 12 px label on the right (a field header); returns (layout, right)."""
    row = hbox(spacing=10)
    row.addWidget(_caption(caption, cap_name), 0, Qt.AlignmentFlag.AlignBottom)
    row.addStretch(1)
    right = label("", wrap=False, name=right_name)
    right.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
    row.addWidget(right, 0, Qt.AlignmentFlag.AlignBottom)
    return row, right


def _note_row(name: str, icon: str = "info-circle", colour: str = theme.ACCENT, text_colour: str = theme.TEXT_2,
              hidden: bool = True):
    """An icon and a 12 px sentence (a note under a field); returns (widget, label).  ``hidden``: it starts
    hidden (never shown before it has a parent: a parentless widget shown is a window of its own)."""
    w = QWidget()
    w.setObjectName(name + "Row")
    h = hbox(w, spacing=8)
    stroke = 1.7 if icon == "alert-circle" else 1.6
    h.addWidget(_svg(icon, 14, colour, stroke, top=2), 0, Qt.AlignmentFlag.AlignTop)
    lb = label("", name=name)
    lb.setStyleSheet(f"font-size:12px; color:{text_colour};")
    h.addWidget(lb, 1)
    if hidden:
        w.hide()
    return w, lb


class _UnitField(QLineEdit):
    """A 40 px text field with its unit ("cm per pixel") inside on the right."""

    def __init__(self, name: str, unit: str = "cm per pixel"):
        super().__init__()
        self.setObjectName(name)
        self.setFixedHeight(40)
        self.unit = QLabel(unit, self)
        self.unit.setObjectName(name + "Unit")
        self.unit.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2}; background:transparent; border:none;")
        self.unit.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.set_border("normal")

    def set_border(self, state: str) -> None:
        colour = {"accent": FIELD_ACCENT, "invalid": theme.DANGER}.get(state, theme.BORDER_STRONG)
        self.setProperty("border", state)
        self.setStyleSheet(f"QLineEdit#{self.objectName()}{{border:1px solid {colour}; border-radius:8px;"
                           f" background:rgba(247,245,251,0.06); color:{theme.TEXT}; font-size:13px; font-weight:400;"
                           f" padding:0 104px 0 12px;}}"
                           f"QLineEdit#{self.objectName()}:focus{{border:1px solid "
                           f"{theme.DANGER if state == 'invalid' else FIELD_ACCENT};}}"
                           f"QLineEdit#{self.objectName()}:disabled{{color:rgba(247,245,251,0.4);}}")

    def set_unit(self, text: str) -> None:
        self.unit.setText(text)
        self._place()

    def _place(self) -> None:
        self.unit.adjustSize()
        self.unit.move(max(0, self.width() - 12 - self.unit.width()), (self.height() - self.unit.height()) // 2)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place()


class _OptionDelegate(QStyledItemDelegate):
    """Drop-down items with a muted label on the right ("1 cm to 1 m", "Finer than the image"); an item that is
    off is drawn muted."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        right = index.data(RIGHT_ROLE)
        if not right:
            return
        painter.save()
        painter.setPen(QColor(170, 164, 181))
        f = painter.font()
        f.setPixelSize(12)
        painter.setFont(f)
        painter.drawText(option.rect.adjusted(0, 0, -10, 0), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                         str(right))
        painter.restore()


class ResolutionCard(QFrame):
    """The Start screen's Resolution card: the image's resolution (from the file, always editable) and the
    resolution the run runs at.  The state is a ``resolution.Choice`` kept in ``ctx.extras`` for the raster (see
    ``ui/flow/resolution.py``)."""

    def __init__(self, on_change=None):
        super().__init__()
        self.setObjectName("resolutionCard")
        self.setProperty("card", True)
        self.on_change = on_change
        self.choice = None
        self._filling = False
        lay = vbox(self, margins=(14, 14, 14, 14), spacing=12)
        lay.addWidget(SectionHeading("Resolution"))
        img = vbox(spacing=6)
        row, self.state_lb = _caption_row("Image resolution", "imageResCaption", "imageResState")
        img.addLayout(row)
        self.image_in = _UnitField("imageResInput")
        self.image_in.setPlaceholderText("e.g. 10")
        self.image_in.textChanged.connect(self._image_edited)
        img.addWidget(self.image_in)
        self.note_w, self.note_lb = _note_row("imageResNote")
        img.addWidget(self.note_w)
        self.error_w, self.error_lb = _note_row("imageResError", "alert-circle", theme.DANGER, theme.DANGER)
        img.addWidget(self.error_w)
        self.file_w = QWidget()
        self.file_w.setObjectName("fileValueRow")
        fh = hbox(self.file_w, spacing=8)
        self.file_lb = label("", wrap=False, name="fileSays")
        self.file_lb.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        fh.addWidget(self.file_lb)
        self.use_file_btn = button(res.USE_FILE, link=True, name="useFileValue")
        self.use_file_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.use_file_btn.setStyleSheet(f"QPushButton{{border:none; background:transparent; color:{theme.ACCENT};"
                                        f" font-size:12px; font-weight:500; padding:0;}}"
                                        f"QPushButton:hover{{color:{theme.TEXT};}}")
        self.use_file_btn.clicked.connect(self.use_file_value)
        fh.addWidget(self.use_file_btn)
        fh.addStretch(1)
        self.file_w.hide()
        img.addWidget(self.file_w)
        lay.addLayout(img)
        run = vbox(spacing=6)
        row, _r = _caption_row("Run at", "runAtCaption", "runAtRight")
        run.addLayout(row)
        self.run_at = QComboBox()
        self.run_at.setObjectName("runAtCombo")
        name_combo(self.run_at, "Run at", "The resolution the run runs at")
        self.run_at.setFixedHeight(40)
        style_combo_popup(self.run_at)
        self.run_at.setItemDelegate(_OptionDelegate(self.run_at))
        self.run_at.setStyleSheet(
            f"QComboBox{{padding:0 36px 0 12px; border-radius:8px; border:1px solid {theme.BORDER_STRONG};"
            f" background:rgba(247,245,251,0.06); color:{theme.TEXT}; font-size:13px; font-weight:400;}}"
            "QComboBox:disabled{color:rgba(247,245,251,0.4); border-color:rgba(247,245,251,0.10);}"
            "QComboBox::drop-down{border:none; width:0px;} QComboBox::down-arrow{image:none; width:0px; height:0px;}")
        self._fill_run_at(["own", "0.6", "0.3", "custom"])
        self.run_at.activated.connect(self._picked)
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(self.run_at)
        left = QLabel(wrap)
        left.setFixedSize(0, 0)
        self.chevron = _svg("chevron-down", 16, theme.TEXT_2, 1.8)
        self.chevron.setParent(wrap)
        wrap.installEventFilter(_ComboOverlay(wrap, left, self.chevron))
        run.addWidget(wrap)
        lay.addLayout(run)
        self.custom_w = QWidget()
        self.custom_w.setObjectName("customBlock")
        cl = vbox(self.custom_w, spacing=6)
        row, self.custom_range = _caption_row("Custom", "customCaption", "customRange")
        self.custom_range.setText(res.CUSTOM_RANGE)
        cl.addLayout(row)
        self.custom_in = _UnitField("customInput")
        self.custom_in.setAccessibleName("Custom resolution, in cm per pixel")
        self.custom_in.textChanged.connect(self._custom_edited)
        cl.addWidget(self.custom_in)
        self.custom_err_w, self.custom_err = _note_row("customError", "alert-circle", theme.DANGER, theme.DANGER)
        cl.addWidget(self.custom_err_w)
        self.custom_w.hide()
        lay.addWidget(self.custom_w)
        self.hint_w, self.hint_lb = _note_row("resolutionHint", hidden=False)
        lay.addWidget(self.hint_w)

    _RUN_AT_TEXT = {"own": "", "0.6": "60 cm", "0.3": "30 cm", "custom": "Custom…"}

    def _fill_run_at(self, keys) -> None:
        """The Run at options for ``keys`` (a preset that reads the same as the image's own is left out), rebuilt
        only when they change."""
        if [self.run_at.itemData(i) for i in range(self.run_at.count())] == list(keys):
            return
        self.run_at.blockSignals(True)
        try:
            self.run_at.clear()
            for key in keys:
                self.run_at.addItem(self._RUN_AT_TEXT[key], key)
        finally:
            self.run_at.blockSignals(False)

    def bind(self, choice, info=None) -> None:
        """Show ``choice`` (the raster's kept state) for ``info`` (the raster as the file has it): the fields are
        filled from it, never the other way round."""
        self.choice = choice
        self.info = info
        self._filling = True
        try:
            self.image_in.setText(choice.image_text)
            self.custom_in.setText(choice.custom_text)
        finally:
            self._filling = False
        self.refresh()

    def _image_edited(self, text: str) -> None:
        if self.choice is None or self._filling:
            return
        self.choice.image_text = text
        self.choice.settle_pick()
        self.refresh()

    def _custom_edited(self, text: str) -> None:
        if self.choice is None or self._filling:
            return
        self.choice.custom_text = text
        self.refresh()

    def _picked(self, i: int) -> None:
        if self.choice is None:
            return
        key = self.run_at.itemData(i)
        item = self.run_at.model().item(i)
        if item is not None and not item.isEnabled():
            return
        self.choice.pick = key
        self.refresh()
        if key == "custom":
            self.custom_in.setFocus(Qt.FocusReason.OtherFocusReason)

    def use_file_value(self) -> None:
        """'Use the file's value': the image field goes back to what the file says."""
        ch = self.choice
        if ch is None or not ch.file_m:
            return
        ch.image_text = res.field_text(ch.file_m, ch.unit)
        ch.settle_pick()
        self._filling = True
        try:
            self.image_in.setText(ch.image_text)
        finally:
            self._filling = False
        self.refresh()

    def pick(self, key: str) -> bool:
        """Choose a Run at option by its key ('own', '0.6', '0.3', 'custom'), as a click on it does."""
        i = self.run_at.findData(key)
        if i < 0:
            return False
        item = self.run_at.model().item(i)
        if item is not None and not item.isEnabled():
            return False
        self.run_at.setCurrentIndex(i)
        self._picked(i)
        return True

    def refresh(self) -> None:
        ch = self.choice
        if ch is None:
            return
        im = ch.image_m()
        self.state_lb.setText(ch.state_label())
        self.state_lb.setStyleSheet(f"font-size:12px; color:{theme.ACCENT if ch.edited else theme.TEXT_2};")
        unit = "m per pixel" if ch.unit == "m" else "cm per pixel"
        self.image_in.set_unit(unit)
        self.image_in.setAccessibleName(f"Image resolution, in {unit}")
        problem = ch.image_problem()
        invalid = problem == res.IMAGE_MIN_TEXT
        self.image_in.set_border("invalid" if invalid else ("accent" if (ch.edited or not ch.file_m) else "normal"))
        note = res.UNKNOWN_TEXT if problem == res.UNKNOWN_TEXT else ch.note()
        self.note_lb.setText(note)
        self.note_w.setVisible(bool(note))
        self.error_lb.setText(res.IMAGE_MIN_TEXT if invalid else "")
        self.error_w.setVisible(invalid)
        self.file_lb.setText(res.file_says(ch.file_m) if ch.file_m else "")
        self.file_w.setVisible(ch.edited)
        ch.settle_pick()
        self._fill_run_at(ch.run_at_keys())
        self.run_at.setEnabled(im is not None)
        model = self.run_at.model()
        for i in range(self.run_at.count()):
            key = self.run_at.itemData(i)
            item = model.item(i)
            if key == "own":
                text, right, on = ch.own_text(), "", True
            elif key == "custom":
                off = im is not None and ch.custom_off()
                text, right, on = "Custom…", (res.FINER_OPTION if off else res.CUSTOM_RANGE), not off
            else:
                off = im is not None and ch.finer(float(key))
                text, right, on = item.text(), (res.FINER_OPTION if off else ""), not off
            item.setText(text)
            item.setData(right, RIGHT_ROLE)
            item.setData(f"{text} · {right}" if right else text, Qt.ItemDataRole.AccessibleTextRole)
            item.setData(right or None, Qt.ItemDataRole.ToolTipRole)
            item.setEnabled(on)
        idx = self.run_at.findData(ch.pick)
        if idx >= 0 and self.run_at.currentIndex() != idx:
            self.run_at.setCurrentIndex(idx)
        custom = ch.pick == "custom" and im is not None
        self.custom_w.setVisible(custom)
        err = ch.custom_problem() if (custom and ch.custom_text.strip()) else ""
        self.custom_err.setText(err)
        self.custom_err_w.setVisible(bool(err))
        self.custom_in.set_border("invalid" if err and ch.custom_text.strip() else ("accent" if custom else "normal"))
        self.hint_lb.setText(ch.hint(getattr(self, "info", None)))
        if callable(self.on_change):
            self.on_change()


@registry.screen("EX-Start")
class ExStart(_ImageryPicker):
    def __init__(self, ctx):
        super().__init__(ctx)
        _connect_project(ctx)
        lay = _scroll_page(self)
        self.update_card = None
        try:
            from .entry import UpdateCard
            self.update_card = UpdateCard(ctx)
            lay.addWidget(self.update_card)
        except ImportError:
            pass
        self._build_picker(lay)
        self.res_card = ResolutionCard(on_change=self._resolution_changed)
        self.res_card.hide()
        lay.addWidget(self.res_card)
        self.status = self.res_card.hint_lb
        self.imagery_error = label("", danger=True, name="imageryError")
        self.imagery_error.hide()
        lay.addWidget(self.imagery_error)
        lay.addWidget(_how_it_works())
        lay.addStretch(1)
        self.start_btn = ArrowButton("Start")
        self.start_btn.setObjectName("startButton")
        self.start_btn.clicked.connect(self.start)
        self.pinned.addWidget(self.start_btn)

    def on_show(self, **kw):
        _connect_project(self.ctx)
        clear_overlay(self.ctx)
        self.fill_combo()
        info = self.ctx.imagery
        ch = res.choice_for(self.ctx, info)
        if ch is not None:
            self.res_card.bind(ch, info)
        self.res_card.setVisible(ch is not None)
        chk = self.ctx.extras.get("imagery_check")
        msg = "" if info is not None or chk is None else (getattr(chk, "message", "") or "")
        self.imagery_error.setText(msg)
        self.imagery_error.setVisible(bool(msg))
        self._resolution_changed()

    def _resolution_changed(self) -> None:
        ch = self.res_card.choice if self.ctx.imagery is not None else None
        ok = self.ctx.imagery is not None and ch is not None and ch.valid
        self.start_btn.setEnabled(ok)
        set_tip(self.start_btn, "" if ok or self.ctx.imagery is None else res.START_TIP)

    def start(self) -> None:
        if self.ctx.current_board != "EX-Start":
            return
        if self.ctx.imagery is not None and res.run_imagery(self.ctx) is not None:
            self.ctx.area = None
            self.ctx.go("EX-DrawArea")


class _WholeImageButton(PillButton):
    """'Use whole image': a 40 px outlined pill with the four-corners icon; pressed (the area is the whole image)
    it is lavender-tinted with a lavender tick."""

    def __init__(self):
        super().__init__("Use whole image")
        self.setObjectName("wholeImageButton")
        self.setCheckable(True)
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(15, 15))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.set_pressed(False)

    def set_pressed(self, on: bool) -> None:
        self.blockSignals(True)
        self.setChecked(bool(on))
        self.blockSignals(False)
        set_tip(self, "The area is the whole image" if on else "Set the area to the whole image")
        if on:
            self.setIcon(icons.icon("check", 15, theme.ACCENT, 1.8))
        else:
            self.setIcon(icons.icon("expand", 15, theme.TEXT, 1.6))
        border = "rgba(200,173,255,0.45)" if on else theme.BORDER_STRONG
        bg = "rgba(139,92,246,0.12)" if on else theme.CARD_BG
        self.setStyleSheet(f"QPushButton{{background:{bg}; color:{theme.TEXT}; border:1px solid {border};"
                           f" padding:0 16px 0 14px; font-size:13px; font-weight:500; text-align:left;}}"
                           f"QPushButton:disabled{{color:rgba(247,245,251,0.35); border-color:{theme.HAIRLINE};}}")


def _area_unit(a, n: int) -> str:
    """'tiles at 60 cm · 1.9 km² drawn' / 'tiles at 20 cm · whole image, 5.4 km²' beside the tile count: the
    resolution the run runs at, then the drawn area, said as such (the tiles' own area is on the line below)."""
    from .map_tools import drawn_km2

    word = ("tile" if n == 1 else "tiles") + f" at {res_label(a.gsd_m)}"
    if getattr(a, "upper_bound", False):
        word += ", at most"
    km = km2_label(drawn_km2(a))
    return f"{word} · whole image, {km}" if a.whole_image else f"{word} · {km} drawn"


NOT_SENT_TEXT = "Tiles with no imagery are not sent or charged."


def large_run_text(ctx, tiles: int, upper_bound: bool = False, area=None) -> str:
    """The Draw screen's note for a large run, '' for a run that is not large (see ``estimate.is_large``).  The
    time is the run check's own estimate (``estimate.takes_for_area``, which adds the extra reading when the run runs
    coarser than the image); without a real one the sentence has none."""
    secs = (estimate.takes_for_area(ctx.settings, area, ctx.meta) if area is not None
            else estimate.takes_seconds(ctx.settings, tiles, ctx.meta))
    if not tiles or not estimate.is_large(tiles, secs):
        return ""
    n = ("up to " if upper_bound else "") + tile_cost_label(tiles)
    lead = (f"Large runs take longer: {estimate.about(secs)} for {n}." if secs is not None
            else "Large runs take longer.")
    return f"{lead} One run can use up to {count(RUN_TILE_LIMIT)} tiles."


def _step_bar(current: int) -> QWidget:
    w = QWidget()
    w.setObjectName("stepBar")
    w.setAccessibleName("Extract steps")
    h = hbox(w, spacing=6)
    for i, t in enumerate(("Draw", "Check", "Extract"), 1):
        cur = i == current
        b = QFrame()
        b.setObjectName(f"step{i}")
        b.setFixedHeight(36)
        b.setProperty("current", cur)
        b.setStyleSheet(f"QFrame{{border:1px solid {'rgba(200,173,255,0.45)' if cur else theme.HAIRLINE};"
                        f" background:{'rgba(139,92,246,0.12)' if cur else 'rgba(247,245,251,0.03)'};"
                        f" border-radius:10px;}} QLabel{{border:none; background:transparent; font-weight:500; font-size:12px;}}")
        bl = hbox(b, margins=(10, 0, 10, 0), spacing=8)
        n = label(str(i), mono=True, wrap=False)
        n.setStyleSheet(f"color:{theme.ACCENT if cur else theme.TEXT_MUTED}; font-family:{theme.FONT_MONO}; font-size:11px;")
        nm = label(t, wrap=False)
        nm.setStyleSheet(f"color:{theme.TEXT if cur else theme.TEXT_2}; font-weight:500; font-size:12px;")
        bl.addWidget(n)
        bl.addWidget(nm, 1)
        h.addWidget(b, 1)
    return w


def kbd(text: str) -> QLabel:
    k = QLabel(text)
    k.setStyleSheet(f"border:1px solid {theme.BORDER_STRONG}; background:{theme.CARD_BG}; border-radius:4px;"
                    f" padding:2px 6px; font-family:{theme.FONT_MONO}; font-size:10px; color:{theme.TEXT};")
    track(k, 0.8, caps=True)
    return k


class _RearmFilter(QObject):
    """Returning to the panel (mouse in, click, focus or window activation) re-arms the rectangle tool."""

    def __init__(self, screen):
        super().__init__(screen)
        self._ref = weakref.ref(screen)

    def eventFilter(self, o, ev):
        if ev.type() in (QEvent.Type.Enter, QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn,
                         QEvent.Type.WindowActivate):
            s = self._ref()
            if s is not None:
                try:
                    s.rearm()
                except RuntimeError:
                    pass
        return False


@registry.screen("EX-DrawArea")
class ExDrawArea(Screen):
    def __init__(self, ctx):
        super().__init__(ctx)
        lay = _scroll_page(self)
        lay.addWidget(_step_bar(1))
        tb = vbox(spacing=6, margins=(2, 14, 2, 2))
        h = label("Draw an area", name="drawHeading")
        h.setStyleSheet("font-size:18px; font-weight:500;")
        tb.addWidget(h)
        self.body = label("Drag a rectangle over the roads you want. Each square is one tile.", muted=True,
                          name="drawBody")
        tb.addWidget(self.body)
        tb.addSpacing(6)
        self.whole_btn = _WholeImageButton()
        self.whole_btn.toggled.connect(lambda _on: self.use_whole_image())
        tb.addWidget(self.whole_btn, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addLayout(tb)
        lay.addWidget(_caption("Imagery", "drawImageryCaption"))
        icard = _row_card(0)
        icard.setObjectName("imageryCard")
        icard.setStyleSheet(icard.styleSheet().replace("QFrame#rowCard", "QFrame#imageryCard"))
        ic = vbox(icard, spacing=0)
        ir = QFrame()
        ir.setObjectName("imageryRow")
        ir.setFixedHeight(38)
        ir.setStyleSheet("QFrame#imageryRow{border:none; border-bottom:1px solid rgba(247,245,251,0.08);"
                         " background:transparent;}")
        ih = hbox(ir, margins=(12, 0, 12, 0), spacing=10)
        ih.addWidget(_svg("image", 15, theme.TEXT_2))
        self.imagery_lb = ElideLabel("", "drawImagery")
        self.imagery_lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        ih.addWidget(self.imagery_lb, 1)
        self.gsd_lb = label("", mono=True, wrap=False, name="drawGsd")
        self.gsd_lb.hide()
        self.zoom_btn = QPushButton("Zoom to")
        self.zoom_btn.setObjectName("zoomToImagery")
        set_tip(self.zoom_btn, "Fit the map to this imagery")
        self.zoom_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zoom_btn.setFlat(True)
        self.zoom_btn.setStyleSheet(f"QPushButton{{border:none; background:transparent; color:{theme.ACCENT}; font-size:12px;"
                                    f" padding:0 2px;}} QPushButton:hover{{color:{theme.TEXT};}}")
        self.zoom_btn.clicked.connect(lambda: zoom_to_imagery(self.ctx))
        ih.addWidget(self.zoom_btn)
        ic.addWidget(ir)
        rrow, self.res_lb, self.change_btn = _resolution_row("drawResolution", self.change_resolution)
        ic.addWidget(rrow)
        lay.addWidget(icard)
        cw, cl = _note_row("resolutionClears", hidden=False)
        cl.setText(res.CLEARS_TEXT)
        lay.addWidget(cw)
        c = card(name="tilesSoFarCard")
        cl = vbox(c, margins=(14, 14, 14, 14), spacing=12)
        cl.addWidget(SectionHeading("Your area"))
        nr = hbox(spacing=10)
        self.count_lb = label("0", mono=True, wrap=False, name="tileCount")
        self.count_lb.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:32px; font-weight:500; color:{theme.TEXT};")
        self.count_lb.setFixedHeight(32)
        nr.addWidget(self.count_lb)
        self.unit_lb = label("", muted=True, wrap=False, name="tilesUnit")
        self.unit_lb.setContentsMargins(0, 0, 0, 3)
        nr.addWidget(self.unit_lb, 1, Qt.AlignmentFlag.AlignBottom)
        head = vbox(spacing=4)
        head.addLayout(nr)
        self.tiles_km_lb = label("", muted=True, name="tilesArea")
        self.tiles_km_lb.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        self.tiles_km_lb.hide()
        head.addWidget(self.tiles_km_lb)
        cl.addLayout(head)
        self.rec_w, self.rec_lb = _note_row("recommendedTiles")
        self.rec_lb.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(self.rec_w)
        usage = vbox(spacing=6)
        br = hbox()
        self.of_lb = label("", muted=True, wrap=False, name="tilesOf")
        self.of_lb.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        self.after_lb = label("", muted=True, wrap=False, name="leftAfter")
        self.after_lb.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:12px; color:{theme.TEXT};")
        br.addWidget(self.of_lb, 1)
        br.addWidget(self.after_lb)
        usage.addLayout(br)
        self.bar = QProgressBar()
        self.bar.setObjectName("tilesBar")
        self.bar.setTextVisible(False)
        usage.addWidget(self.bar)
        cl.addLayout(usage)
        self.large_row = QWidget()
        lr = hbox(self.large_row, spacing=8)
        lr.addWidget(_svg("info-circle", 14, theme.ACCENT, 1.6, top=2), 0, Qt.AlignmentFlag.AlignTop)
        self.large_lb = label("", muted=True, name="largeRunNote")
        self.large_lb.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        lr.addWidget(self.large_lb, 1)
        self.large_row.hide()
        cl.addWidget(self.large_row)
        self.foot = label("", muted=True, name="drawFootnote")
        self.foot.hide()
        cl.addWidget(self.foot)
        lay.addWidget(c)
        lay.addStretch(1)
        hints = hbox(spacing=8)
        hints.addStretch(1)
        for w in (kbd("Esc"), label("Cancel", muted=True, wrap=False)):
            hints.addWidget(w)
        hints.addStretch(1)
        lay.addLayout(hints)
        btns = hbox(spacing=8)
        self.redraw_btn = pill_button("Redraw", name="redrawButton")
        self.redraw_btn.setAccessibleName("Redraw area")
        self.redraw_btn.setStyleSheet(self.redraw_btn.styleSheet() + "QPushButton{padding:0 20px;}")
        self.redraw_btn.clicked.connect(self.redraw)
        self.next_btn = ArrowButton("Next")
        self.next_btn.setObjectName("nextButton")
        self.next_btn.clicked.connect(self.next)
        btns.addWidget(self.redraw_btn)
        btns.addWidget(self.next_btn, 1)
        self.pinned.addLayout(btns)
        self.tool = None
        self._prev_tool = None
        self.pending = None
        self._filter = None
        self._sig = SignalGroup()
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._esc.activated.connect(self.exit_tool)

    def on_show(self, **kw):
        from .map_tools import DrawAreaTool

        ctx = self.ctx
        info = ctx.imagery
        if info is None:
            QTimer.singleShot(0, lambda: ctx.go("EX-Start"))
            return
        self._release_tool(keep=False)
        clear_overlay(ctx)
        canvas = ctx.iface.mapCanvas()
        cur = canvas.mapTool()
        self._prev_tool = cur if not isinstance(cur, DrawAreaTool) else None
        self._note = ctx.extras.pop("draw_note", "")
        run_info = res.run_imagery(ctx)
        if run_info is None:
            QTimer.singleShot(0, lambda: ctx.go("EX-Start"))
            return
        self.run_info = run_info
        self.tool = DrawAreaTool(canvas, run_info, ctx.tiles_left, panel=getattr(ctx, "panel", None))
        self._sig.connect(self.tool.changed, self.refresh)
        self._sig.connect(self.tool.drawn, self._drawn)
        self._sig.connect(self.tool.cancelled, self.exit_tool)
        self._sig.connect(ctx.account_changed, self.refresh)
        ctx.read_balance()
        self.pending = None
        if ctx.area is None:
            zoom_to_imagery(ctx, info, only_if_out_of_view=True)
        if ctx.area is not None and ctx.area.tile_count:
            self.pending = ctx.area
            self.tool.show_area(ctx.area)
        self.imagery_lb.setText(info.name)
        self.gsd_lb.setText(res_label(run_info.gsd_m).upper())
        self.res_lb.setText(res.summary(run_info.gsd_m, run_info.image_gsd_m or run_info.gsd_m))
        if self._filter is None:
            self._filter = _RearmFilter(self)
            self.installEventFilter(self._filter)
        canvas.setMapTool(self.tool)
        self.refresh()

    def on_hide(self):
        keep = self.pending is not None and self.ctx.area is self.pending
        self._release_tool(keep=keep)

    def on_panel_hidden(self) -> None:
        """The panel was closed or hidden on this screen: keep the rectangle, give the canvas back."""
        if self.tool is None:
            return
        if self.pending is not None:
            self.ctx.area = self.pending
        self.on_hide()

    def on_panel_shown(self) -> None:
        if self.tool is None and self.ctx.current_board == "EX-DrawArea" and self.ctx.imagery is not None:
            self.on_show()

    def rearm(self) -> bool:
        """Put the rectangle tool back on the canvas (another map tool took over while the panel was open)."""
        t = self.tool
        if t is None or self.ctx.current_board != "EX-DrawArea":
            return False
        canvas = self.ctx.iface.mapCanvas()
        if canvas.mapTool() is t:
            return False
        canvas.setMapTool(t)
        return True

    def _release_tool(self, keep: bool = False) -> None:
        """Take the tool off the canvas.  ``keep``: the rectangle stays on the map as the run check's preview."""
        t = self.tool
        self.tool = None
        self._sig.disconnect_all()
        if self._filter is not None:
            try:
                self.removeEventFilter(self._filter)
                self._filter.deleteLater()
            except RuntimeError:
                pass
            self._filter = None
        if t is None:
            return
        canvas = self.ctx.iface.mapCanvas()
        try:
            if keep and self.pending is not None:
                self.ctx.extras["overlay"] = t.detach_preview()
            else:
                t.clear()
            if canvas.mapTool() is t:
                if self._prev_tool is not None:
                    canvas.setMapTool(self._prev_tool)
                else:
                    canvas.unsetMapTool(t)
        except RuntimeError:
            pass

    def refresh(self) -> None:
        t = self.tool
        n = t.count if t else 0
        a = (t.area if t else None) or None
        upper = bool(a is not None and n and getattr(a, "upper_bound", False))
        left = self.ctx.tiles_left()
        over_balance = bool(left is not None and n > left and not upper)
        over = over_balance or over_run_limit(n)
        self.count_lb.setText(count(n))
        self.count_lb.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:32px; font-weight:500;"
                                    f" color:{theme.DANGER if over else theme.TEXT};")
        self.count_lb.setProperty("over", over)
        self.unit_lb.setText(_area_unit(a, n) if a is not None and n else "")
        cover = f"The tiles cover {km2_label(a.area_km2)}." if a is not None and n else ""
        self.tiles_km_lb.setText(f"{cover} {NOT_SENT_TEXT}" if upper else cover)
        self.tiles_km_lb.setVisible(bool(a is not None and n))
        self.bar.setRange(0, max(left or 0, 1))
        self.bar.setValue(min(n, left or 0))
        up = "up to " if upper else ""
        self.bar.setAccessibleName(f"{up}{count(n)} of {tile_cost_label(left)}" if left is not None
                                   else up + tile_cost_label(n))
        self.bar.setProperty("over", over_balance)
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)
        if left is not None and n:
            self.of_lb.setText(f"{'Up to ' if upper else ''}{count(n)} of your {tile_cost_label(left)}")
            if upper:
                self.after_lb.setText(f"up to {count(n - left)} over" if n > left else f"{count(left - n)} or more left")
            else:
                self.after_lb.setText(f"{count(n - left)} over" if over_balance else f"{count(left - n)} left after")
        else:
            self.of_lb.setText(f"Your {tile_cost_label(left)}" if left is not None else "")
            self.after_lb.setText("")
        self.after_lb.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:12px;"
                                    f" color:{theme.DANGER if over_balance else theme.TEXT};")
        large = large_run_text(self.ctx, n, upper, a) if a is not None else ""
        self.large_lb.setText(large)
        self.large_row.setVisible(bool(large))
        a60 = res.recommended(self.ctx, a) if (a is not None and n) else None
        self.rec_lb.setText(_recommended_html(a60) if a60 is not None else "")
        self.rec_w.setVisible(a60 is not None)
        if t is not None and (t.area is None or t.count == 0):
            self.pending = None
        outside = bool(t is not None and t.area is not None and t.count == 0 and t.points)
        if t is not None and t.area is not None:
            self._note = ""
        note = getattr(self, "_note", "")
        self.foot.setText(note if (note and not outside)
                          else outside_text(self.ctx.imagery.name if self.ctx.imagery is not None else ""))
        self.foot.setVisible(outside or bool(note))
        self.whole_btn.set_pressed(bool(t is not None and t.whole and self.pending is not None))
        self.whole_btn.setEnabled(t is not None)
        self.next_btn.setEnabled(self.pending is not None)
        self.redraw_btn.setEnabled(self.pending is not None or bool(t and t.area is not None))

    def _drawn(self, geom: QgsGeometry) -> None:
        """A rectangle was released on the map."""
        t = self.tool
        a = t.area if t is not None else None
        self.pending = a if (a is not None and a.tile_count) else None
        self.refresh()

    def exit_tool(self) -> None:
        """Esc: cancel and go back to the imagery."""
        self.pending = None
        self.ctx.area = None
        self.ctx.go("EX-Start")

    def change_resolution(self) -> None:
        """'Change' beside the resolution: back to the Resolution card; the area is cleared."""
        if self.ctx.current_board != "EX-DrawArea":
            return
        self.pending = None
        change_resolution(self.ctx)

    def use_whole_image(self) -> bool:
        """'Use whole image': the area is the selected raster's full extent, tiled exactly as a rectangle dragged
        over the whole raster (the tool feeds the extent through the drawn rectangle's path).  The map shows all of
        it; Next goes on as usual (to the run check, or to the not-enough-tiles page)."""
        t = self.tool
        info = self.ctx.imagery
        if t is None or info is None or self.ctx.current_board != "EX-DrawArea":
            self.refresh()
            return False
        from qgis.core import QgsRectangle

        canvas = self.ctx.iface.mapCanvas()
        r = crs_util.to_canvas_rect(canvas, QgsRectangle(*info.extent), info.crs)
        if not r.isNull() and not canvas.extent().contains(r):
            zoom_to_imagery(self.ctx, info)
        self.rearm()
        ok = t.use_whole_image()
        self.refresh()
        return bool(ok and self.pending is not None)

    def redraw(self) -> None:
        """Forget the rectangle (or the whole image) and draw again."""
        t = self.tool
        if t is None:
            return
        self.pending = None
        self.ctx.area = None
        t.reset()
        self.foot.hide()
        self.rearm()
        self.refresh()

    def next(self) -> bool:
        """Next: the rectangle becomes the run's area; the run check opens (or the over-budget page).

        "Not enough tiles" is never said on an old balance.  When the area is more than the balance this screen knows
        and that figure is not fresh, the balance is read from the service first and the area is judged on the answer
        (Next waits for it; a fresh balance goes on at once)."""
        a = self.pending
        if a is None or self.tool is None or self.ctx.current_board != "EX-DrawArea":
            return False
        self.ctx.area = a
        left = self.ctx.tiles_left()
        if left is not None and a.tile_count > left and not a.upper_bound and not self.ctx.balance_fresh():
            self.next_btn.setEnabled(False)
            ref = weakref.ref(self)

            def judged():
                me = ref()
                if me is None:
                    return
                try:
                    if me.ctx.current_board == "EX-DrawArea" and me.ctx.area is a:
                        me.ctx.go("EX-Setup")
                    else:
                        me.refresh()
                except RuntimeError:
                    pass
            self.ctx.read_balance(judged)
            return True
        self.ctx.go("EX-Setup")
        return True


class _SetupBase(Screen):
    def _locked_inputs(self, lay: QVBoxLayout) -> None:
        blk = vbox(spacing=6)
        blk.addWidget(_caption("Imagery, resolution and area · locked for this run", "lockedCaption"))
        c = _row_card(0)
        cl = vbox(c, spacing=0)
        r1 = QFrame()
        r1.setObjectName("lockRow1")
        r1.setFixedHeight(38)
        r1.setStyleSheet("QFrame#lockRow1{border:none; border-bottom:1px solid rgba(247,245,251,0.08);"
                         " background:transparent;}")
        h1 = hbox(r1, margins=(12, 0, 12, 0), spacing=10)
        h1.addWidget(_svg("image", 15, theme.TEXT_2))
        self.imagery_lb = ElideLabel("", "lockedImagery")
        h1.addWidget(self.imagery_lb, 1)
        self.lock_icon = _svg("padlock", 13, theme.TEXT_2)
        h1.addWidget(self.lock_icon)
        r2 = QFrame()
        r2.setObjectName("lockRow2")
        r2.setFixedHeight(38)
        r2.setStyleSheet("QFrame#lockRow2{border:none; background:transparent;}")
        h2 = hbox(r2, margins=(12, 0, 12, 0), spacing=10)
        h2.addWidget(_svg("map-polygon", 15, theme.TEXT_2))
        self.area_lb = label("", wrap=False, name="lockedArea")
        h2.addWidget(self.area_lb, 1)
        self.redraw = button("Redraw", link=True, name="redrawLink")
        self.redraw.setAccessibleName("Redraw area")
        self.redraw.setStyleSheet(f"QPushButton{{border:none; color:{theme.ACCENT}; font-weight:500;"
                                  f" text-decoration:none;}}")
        self.redraw.clicked.connect(lambda: self.ctx.go("EX-DrawArea"))
        h2.addWidget(self.redraw)
        rrow, self.res_lb, self.change_btn = _resolution_row("lockedResolution", self._change_resolution,
                                                             rule_below=True)
        cl.addWidget(r1)
        cl.addWidget(rrow)
        cl.addWidget(r2)
        blk.addWidget(c)
        cw, cn = _note_row("resolutionClears", hidden=False)
        cn.setText(res.CLEARS_TEXT)
        blk.addWidget(cw)
        lay.addLayout(blk)

    def _change_resolution(self) -> None:
        if self.ctx.current_board in ("EX-Setup", "EX-OverBudget"):
            change_resolution(self.ctx)

    def _fill_inputs(self) -> None:
        a, i = self.ctx.area, self.ctx.imagery
        if a is None or i is None:
            return
        from .map_tools import drawn_km2

        self.imagery_lb.setText(i.name)
        self.res_lb.setText(res.area_summary(a))
        kind = "Whole image" if a.whole_image else "Drawn area"
        self.area_lb.setText(f"{kind} · {km2_label(drawn_km2(a))} · {area_tiles_label(a)}")

    def _stat(self, caption: str, value: str, name: str, first: bool = False) -> QWidget:
        """One cell of the 3-column run-check grid (padding 10, a left rule on all but the first)."""
        w = QFrame()
        w.setObjectName(name)
        w.setStyleSheet("QFrame#%s{border:none; %s}" % (name, "" if first else f"border-left:1px solid {theme.HAIRLINE};"))
        l = vbox(w, margins=(0 if first else 10, 10, 10, 10), spacing=4)
        cap = _caption(caption, name + "Caption")
        l.addWidget(cap)
        w.caption = cap
        v = label(value, mono=True, wrap=False, name=name + "Value")
        v.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:18px; font-weight:500; color:{theme.TEXT};"
                        " border:none;")
        l.addWidget(v)
        w.value = v
        return w

    def _stat_grid(self, *cells: QWidget) -> QFrame:
        """Top and bottom hairlines around equal-width cells (always three columns; a missing one stays empty)."""
        g = QFrame()
        g.setObjectName("statGrid")
        g.setStyleSheet(f"QFrame#statGrid{{border:none; border-top:1px solid {theme.HAIRLINE};"
                        f" border-bottom:1px solid {theme.HAIRLINE};}}")
        h = hbox(g, spacing=0)
        for c in cells:
            h.addWidget(c, 1)
        for _ in range(3 - len(cells)):
            h.addStretch(1)
        return g



@registry.screen("EX-Setup")
class ExSetup(_SetupBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        lay = _scroll_page(self)
        h = label("Run check", name="runCheckHeading")
        h.setStyleSheet("font-size:18px; font-weight:500; padding:0 2px 2px;")
        lay.addWidget(h)
        self._locked_inputs(lay)
        rc = card(name="runCheckCard")
        rl = vbox(rc, margins=(14, 14, 14, 14), spacing=12)
        rl.addWidget(SectionHeading("This run"))
        self.s_tiles = self._stat("Tiles", "0", "statTiles", first=True)
        self.s_img = self._stat("Runs at", "", "statImagery")
        self.s_takes = self._stat("Takes", "", "statTakes")
        self.stat_grid = self._stat_grid(self.s_tiles, self.s_img, self.s_takes)
        rl.addWidget(self.stat_grid)
        br = hbox()
        self.uses_lb = label("", muted=True, wrap=False, name="usesTiles")
        self.uses_lb.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        self.after_lb = label("", muted=True, wrap=False, name="leftAfter")
        self.after_lb.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:12px; color:{theme.TEXT};")
        br.addWidget(self.uses_lb, 1)
        br.addWidget(self.after_lb)
        rl.addLayout(br)
        self.meter = Meter()
        self.meter.setObjectName("budgetMeter")
        rl.addWidget(self.meter)
        self.note = label("", muted=True, name="runNote")
        self.note.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        nrow = hbox(spacing=8)
        nrow.addWidget(_svg("check", 14, theme.SUCCESS, 1.8, top=2), 0, Qt.AlignmentFlag.AlignTop)
        nrow.addWidget(self.note, 1)
        self.note_row = nrow
        rl.addLayout(nrow)
        self.rec_w, self.rec_lb = _note_row("recommendedTiles")
        self.rec_lb.setTextFormat(Qt.TextFormat.RichText)
        rl.addWidget(self.rec_w)
        lay.addWidget(rc)
        lay.addStretch(1)
        btns = hbox(spacing=8)
        self.back_btn = pill_button("Back", name="backButton")
        self.back_btn.setStyleSheet(self.back_btn.styleSheet() + "QPushButton{padding:0 20px;}")
        self.back_btn.clicked.connect(lambda: ctx.go("EX-DrawArea"))
        self.extract_btn = ArrowButton("Extract lines")
        self.extract_btn.setObjectName("extractButton")
        self.extract_btn.clicked.connect(self.extract)
        btns.addWidget(self.back_btn)
        btns.addWidget(self.extract_btn, 1)
        self.pinned.addLayout(btns)

    def on_show(self, **kw):
        a, i = self.ctx.area, self.ctx.imagery
        if a is None or i is None:
            QTimer.singleShot(0, lambda: self.ctx.go("EX-Start"))
            return
        self._fill_inputs()
        n, left = a.tile_count, self.ctx.tiles_left()
        self.s_tiles.value.setText(count(n))
        self.s_tiles.caption.setText("TILES, AT MOST" if a.upper_bound else "TILES")
        self.s_img.value.setText(res_label(a.gsd_m))
        _show_takes(self, n)
        a60 = res.recommended(self.ctx, a)
        self.rec_lb.setText(_recommended_html(a60) if a60 is not None else "")
        self.rec_w.setVisible(a60 is not None)
        from .map_tools import drawn_km2

        set_tip(self.extract_btn, f"{area_tiles_label(a)} · {km2_label(drawn_km2(a))} at {res_label(a.gsd_m)}")
        self._fill_balance()
        if left is None:
            self.ctx.refresh_account()
        else:
            self.ctx.read_balance()
        if not getattr(self, "_acct_hooked", False):
            self._acct_hooked = True
            ref = weakref.ref(self)

            def again():
                me = ref()
                if me is None or me.ctx.current_board != "EX-Setup" or me.ctx.tiles_left() is None:
                    return
                try:
                    if not me.ctx.dialogs and route_setup(me.ctx) != "EX-Setup":
                        me.ctx.go("EX-Setup")
                    else:
                        me._fill_balance()
                except Exception:
                    pass
            self.ctx.account_changed.connect(again)

    def _fill_balance(self) -> None:
        """The balance rows, the note and Extract (the TAKES cell and the rest stay).

        For an upper bound the rows say "up to", the note says that tiles with no imagery are not sent or charged, and
        Extract stays on even past the balance: the run reads the tiles and is judged on the exact count."""
        a = self.ctx.area
        if a is None:
            return
        n, left = a.tile_count, self.ctx.tiles_left()
        upper = bool(getattr(a, "upper_bound", False))
        up = "up to " if upper else ""
        self.uses_lb.setText(f"Uses {up}{count(n)} of your {tile_cost_label(left)}" if left is not None
                             else f"Uses {up}{tile_cost_label(n)}")
        if left is None:
            self.after_lb.setText("")
        elif upper:
            self.after_lb.setText(f"up to {count(n - left)} over" if n > left else f"{count(left - n)} or more left")
        else:
            self.after_lb.setText(f"{count(left - n)} left after")
        self.meter.setAccessibleName(f"{up}{count(n)} of {tile_cost_label(left)}" if left is not None
                                     else up + tile_cost_label(n))
        self.meter.set_segments([(min(1.0, n / left) if left else 0, theme.ACCENT)])
        if left is None:
            self.note.setText("Tile balance unknown while offline.")
        elif upper and n > left:
            self.note.setText(f"{NOT_SENT_TEXT} If more than {tile_cost_label(left)} hold imagery, the run won't start.")
        elif upper:
            self.note.setText(f"{NOT_SENT_TEXT} You have enough tiles for the whole area.")
        else:
            self.note.setText("You have enough tiles for the whole area.")
        self.extract_btn.setEnabled((upper or left is None or n <= left) and not over_run_limit(n))

    def extract(self) -> None:
        ctx = self.ctx
        if ctx.current_board != "EX-Setup":
            return
        from ...cloud import terms

        need = terms.notice_needed(ctx)
        if need:
            ctx.go("EN-BeforeStart", on_agree=lambda: start_run(ctx), on_decline=None,
                   terms_updated=need == terms.UPDATED)
        else:
            start_run(ctx)


def _show_takes(screen, tiles: int) -> None:
    """The TAKES cell: a real estimate only (the service's rate, never faster than this plugin's measured rate: the
    slower of the two when both exist; ``estimate.takes_for_area`` adds the extra reading when the run runs coarser than
    the image); otherwise the cell is gone and the other two stay."""
    ctx = screen.ctx
    a = ctx.area
    secs = (estimate.takes_for_area(ctx.settings, a, ctx.meta) if a is not None and a.tile_count == tiles
            else estimate.takes_seconds(ctx.settings, tiles, ctx.meta))
    txt = estimate.fmt(secs) if secs is not None else ""
    screen.s_takes.value.setText(txt)
    screen.s_takes.setVisible(bool(txt))


def _show_left(screen, run) -> None:
    """EX-Running's LEFT: the service's ETA, never less than the measured rate (this run's own seconds per finished
    tile, else the slower of the service's and the recorded per-tile time), with the upload still to do taken into
    account (``RunController.with_upload_left``); hidden without a real value or once nothing is left to run."""
    ctx = screen.ctx
    done = run.tiles_done
    own = (run.elapsed_s / done) if (done >= 2 and run.elapsed_s) else None
    secs = (estimate.left_seconds(ctx.settings, ctx.meta, run.tiles_total - done, run.eta_s, own)
            if run.running else None)
    if run.running:
        secs = run.with_upload_left(secs)
    txt = estimate.fmt_left(secs)
    _put(screen.left, txt)
    if screen.left_box.isVisibleTo(screen) != bool(txt):
        screen.left_box.setVisible(bool(txt))


def _put(lb, text: str) -> None:
    """A label is set only when its text changes.  Every change is an event for assistive tools (UI Automation on
    Windows), and the Running screen is refreshed several times a second during a run."""
    if lb.text() != text:
        lb.setText(text)


def _name(w, name: str) -> None:
    """As ``_put``, for an accessible name (Qt announces every set, even to the same name)."""
    if w.accessibleName() != name:
        w.setAccessibleName(name)


@registry.screen("EX-OverBudget")
class ExOverBudget(_SetupBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        lay = _scroll_page(self)
        self._locked_inputs(lay)
        self.lock_icon.hide()
        rc = card(name="runCheckCard")
        rc.setStyleSheet("QFrame#runCheckCard{border:1px solid rgba(248,113,113,0.35); border-radius:12px;"
                         f" background:{theme.CARD_BG};}}")
        rl = vbox(rc, margins=(14, 14, 14, 14), spacing=12)
        self.heading = SectionHeading("Not enough tiles")
        rl.addWidget(self.heading)
        self.s_tiles = self._stat("Tiles", "0", "statTiles", first=True)
        self.s_img = self._stat("Runs at", "", "statImagery")
        self.s_takes = self._stat("Takes", "", "statTakes")
        rl.addWidget(self._stat_grid(self.s_tiles, self.s_img, self.s_takes))
        self.meter = Meter(8)
        self.meter.setObjectName("budgetMeter")
        rl.addWidget(self.meter)
        cap = hbox()
        self.left_lb = label("", muted=True, wrap=False, name="barLeft")
        self.left_lb.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        self.over_lb = label("", danger=True, wrap=False, name="barOver")
        self.over_lb.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:12px; color:{theme.DANGER};")
        cap.addWidget(self.left_lb, 1)
        cap.addWidget(self.over_lb)
        rl.addLayout(cap)
        ab = QFrame()
        ab.setObjectName("overAlertBox")
        ab.setStyleSheet("QFrame#overAlertBox{background:rgba(239,68,68,0.08); border:1px solid rgba(248,113,113,0.35);"
                         " border-radius:10px;} QLabel{border:none; background:transparent;}")
        ah = hbox(ab, margins=(12, 10, 12, 10), spacing=8)
        ah.addWidget(_svg("alert-circle", 15, theme.DANGER, 1.7, top=2), 0, Qt.AlignmentFlag.AlignTop)
        self.alert = label("", name="overAlert")
        self.alert.setStyleSheet(f"color:{theme.TEXT}; font-size:13px; font-weight:400;")
        ah.addWidget(self.alert, 1)
        rl.addWidget(ab)
        self.rec_w, self.rec_lb = _note_row("recommendedTiles")
        self.rec_lb.setTextFormat(Qt.TextFormat.RichText)
        self.rec_lb.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse
                                            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.rec_lb.linkActivated.connect(lambda _href: self._change_resolution())
        from ..widgets import link_color

        link_color(self.rec_lb, theme.ACCENT)
        rl.addWidget(self.rec_w)
        self.quota_btn = pill_button("Request quota increase", name="requestQuotaButton")
        self.quota_btn.setFixedHeight(44)
        self.quota_btn.setStyleSheet("QPushButton{background:rgba(139,92,246,0.18); color:#f7f5fb;"
                                     " border:1px solid rgba(200,173,255,0.5); font-weight:500; font-size:14px;}")
        self.quota_btn.clicked.connect(lambda: ctx.open_url(QUOTA_MAILTO))
        rl.addWidget(self.quota_btn)
        from .contact import ContactLine

        rl.addWidget(ContactLine(SUPPORT_EMAIL, name="quotaContact"))
        lay.addWidget(rc)
        lay.addStretch(1)
        self.smaller_btn = ArrowButton("Draw a smaller area")
        self.smaller_btn.setObjectName("drawSmallerButton")
        self.smaller_btn.clicked.connect(lambda: ctx.go("EX-DrawArea"))
        self.pinned.addWidget(self.smaller_btn)
        self.plans_btn = button("Plan details", link=True, name="seePlansButton")
        self.plans_btn.clicked.connect(lambda: ctx.go("ST-Billing"))
        self.pinned.addWidget(self.plans_btn, 0, Qt.AlignmentFlag.AlignHCenter)

    def on_show(self, **kw):
        a, i = self.ctx.area, self.ctx.imagery
        if a is None or i is None:
            QTimer.singleShot(0, lambda: self.ctx.go("EX-Start"))
            return
        self._render()
        if not getattr(self, "_acct_hooked", False):
            self._acct_hooked = True
            ref = weakref.ref(self)

            def again():
                me = ref()
                if me is not None:
                    me._balance_changed()
            self.ctx.account_changed.connect(again)
            self._poll = QTimer(self)
            self._poll.timeout.connect(lambda: self.ctx.refresh_account() if not self.ctx.account_loading else None)
        if self.limit == "balance":
            self._poll.start(OVER_BUDGET_POLL_MS)
            self.ctx.read_balance()
        else:
            self._poll.stop()

    def on_hide(self) -> None:
        poll = getattr(self, "_poll", None)
        if poll is not None:
            poll.stop()
        super().on_hide()

    def _balance_changed(self) -> None:
        ctx = self.ctx
        if ctx.current_board != "EX-OverBudget" or ctx.area is None or ctx.imagery is None:
            return
        try:
            if route_setup(ctx) == "EX-OverBudget":
                self._render()
            elif not ctx.dialogs:
                ctx.go("EX-Setup")
            else:
                self._render_fits()
        except RuntimeError:
            pass

    def _render_fits(self) -> None:
        """The balance now covers the area while a flow dialog is open: the run check opens when it closes."""
        for d in list(self.ctx.dialogs.values()):
            try:
                if not d.property("_trb_fits_hook"):
                    d.setProperty("_trb_fits_hook", True)
                    d.finished.connect(self._after_dialog)
            except (RuntimeError, TypeError):
                pass

    def _after_dialog(self, *_a) -> None:
        QTimer.singleShot(0, self._balance_changed)

    def _render(self) -> None:
        a, i = self.ctx.area, self.ctx.imagery
        if a is None or i is None:
            return
        self._fill_inputs()
        n, bal = a.tile_count, self.ctx.tiles_left()
        upper = bool(getattr(a, "upper_bound", False))
        over_run = over_run_limit(n)
        over_bal = (not upper and bal is not None and n > bal) or not over_run
        left = bal or 0
        self.limit = "balance" if over_bal and (not over_run or left <= RUN_TILE_LIMIT) else "run"
        lim = left if self.limit == "balance" else RUN_TILE_LIMIT
        over = max(0, n - lim)
        need = f"This area needs {area_tiles_label(a)}."
        self.s_tiles.value.setText(f"{count(n)}")
        self.s_img.value.setText(res_label(a.gsd_m))
        _show_takes(self, n)
        a60 = res.recommended(self.ctx, a)
        self.rec_lb.setText(_recommended_html(a60, link=True) if a60 is not None else "")
        self.rec_w.setVisible(a60 is not None)
        self.heading.text.setText(("Not enough tiles" if over_bal else "Too many tiles for one run").upper())
        if self.limit == "balance":
            self.meter.setAccessibleName(f"{need} {count(left)} {'is' if left == 1 else 'are'} left, so it is {count(over)} over.")
            self.left_lb.setText(f"{count(left)} left this month")
        else:
            self.meter.setAccessibleName(f"{need} One run can use up to {count(lim)}, so it is {count(over)} over.")
            self.left_lb.setText(f"{count(lim)} per run")
        self.meter.set_segments([(lim / n if n else 0, theme.ACCENT), (over / n if n else 0, theme.DANGER_STRONG)])
        self.over_lb.setText(f"{count(over)} over")
        per_run = f"one run can use up to {count(RUN_TILE_LIMIT)} tiles"
        if over_bal and over_run:
            self.alert.setText(f"{need} You have {count(left)} left, and {per_run}.")
        elif over_run:
            self.alert.setText(f"{need} {per_run[0].upper()}{per_run[1:]}.")
        else:
            self.alert.setText(f"{need} You have {count(left)} left.")
        self.quota_btn.setVisible(over_bal)


def start_run(ctx) -> Optional[RunController]:
    """Create the run from ctx.imagery / ctx.area and show EX-Running (called by Extract / after consent)."""
    if not imagery_in_project(ctx) or ctx.area is None:
        refuse_stale_imagery(ctx)
        return None
    old = ctx.run
    if old is not None:
        old.dispose()
    run = RunController(ctx, ctx.imagery, ctx.area, list(ALL_CLASSES))
    clear_overlay(ctx)
    ctx.run = run
    wire_run(ctx, run)
    ctx.go("EX-Running")
    run.begin()
    return run


def wire_run(ctx, run: RunController) -> None:
    ref = weakref.ref(ctx)

    def phase(ph: str):
        c = ref()
        if c is None or c.run is not run:
            return
        if ph == "extracting" and c.current_board not in ("EX-Running",):
            c.go("EX-Running")

    def finished(state: str):
        c = ref()
        if c is None or c.run is not run:
            return
        s = c.panel.screen("EX-Running") if c.panel is not None and c.current_board == "EX-Running" else None
        if s is not None and s.holds_run_end(run):
            return
        after_run_end(c, run, state)

    def problem(kind: str, err):
        c = ref()
        if c is None or c.run is not run:
            return
        if kind == "over_budget":
            left = (err.extra or {}).get("tiles_left")
            if left is not None:
                c.set_tiles_left(left)
            clear_overlay(c)
            c.go("EX-Setup")
        elif kind == "deleted":
            clear_overlay(c)
            c.go("EX-Failed", kind="deleted")
        elif kind == "no_imagery":
            run.dispose()
            c.run = None
            clear_overlay(c)
            c.area = None
            name = c.imagery.name if c.imagery is not None else "the imagery"
            c.extras["draw_note"] = f"That area of {name} holds no imagery. Nothing was charged."
            c.go("EX-DrawArea")
        elif kind == "consent_required":
            consent = ((c.account or {}).get("consent"))
            if isinstance(consent, dict):
                consent["accepted_terms_version"] = None

            def declined():
                """The terms were declined: nothing was sent and the Running screen has nothing to show, so go back to
                the run check."""
                cc = ref()
                if cc is None or cc.run is not run:
                    return
                run.dispose()
                cc.run = None
                clear_overlay(cc)
                cc.go("EX-Setup")
            c.go("EN-BeforeStart",
                 on_agree=lambda: (run.begin() if not run.run_id else run.retry_start()), on_decline=declined,
                 terms_updated=False)
        else:
            c.go("EX-Failed", kind="busy" if kind == "busy" else "error")

    def guard(*_a):
        """Ask the leave guard to look again: it holds QGIS's ways of leaving while this run is in flight, until its
        end is handed over (``LeaveGuard.run_at_stake``)."""
        c = ref()
        if c is not None and c.run is run:
            guard_run_soon(c)

    run.phase.connect(phase)
    run.finished.connect(finished)
    run.problem.connect(problem)
    run.changed.connect(guard)
    run.finished.connect(guard)


def after_run_end(c, run: RunController, state: str) -> None:
    """Where the flow goes when a run ended (straight away, or after the Stop dialog's Continue).

    Once per end of the run, whichever path brings it first (the run's ``finished``, the Stop dialog's Continue, its Esc
    a moment later): a second call for the same end does nothing, so it never opens the review again over the one on
    screen (resetting what was ticked or deleted), never takes the panel back to EX-Failed or Start after New area, and
    never fetches the result twice.  A run set going again (Try again) ends anew.  Asking the service to delete its copy
    of the result is the run's own job, at its end (``RunController._at_service_end``)."""
    if run.end_taken == run.ends:
        return
    run.end_taken = run.ends
    broken = getattr(run, "upload_broken", None)
    if (broken is not None or getattr(run, "stop_unreachable", False)) and state in ("stopped", "failed"):
        net = run.connection_lost or broken is None or broken.code == "network_error"
        c.go("EX-Failed", kind="connection" if net else "stopped")
        return
    if state in ("done", "stopped"):
        if run.tiles_done == 0:
            clear_overlay(c)
            c.go("EX-Start")
            return
        open_review(c, run)
    elif state == "failed":
        c.go("EX-Failed", kind="connection" if run.connection_lost else "stopped")
    elif state == "nothing_found":
        c.extras["nothing_tiles"] = int(getattr(run, "tiles_used", 0) or 0)
        c.go("EX-Failed", kind="nothing")


_NEW_WORK = ("EX-Start", "EX-NoImagery", "EX-DrawArea", "EX-Setup", "EX-OverBudget")


def open_review(ctx, run: RunController) -> None:
    """Open the review on the run's result: the one fetched when its end was read, or, when the service has no
    result for it (410 ``run_data_deleted``, no answer), the lines the plugin kept from its events.

    Once per end of the run: a second call while the result is on its way, or while the review of this run is open,
    does nothing.  A result that arrives after the run was let go (the project changed) or ended anew, or after the user
    went on to a new area, is dropped."""
    end = run.ends
    s = getattr(ctx, "review", None)
    if run.review_fetch == end or (s is not None and not getattr(s, "closed", False) and bool(run.run_id)
                                   and getattr(s, "run_id", None) == run.run_id):
        return
    run.review_fetch = end
    board0 = ctx.current_board

    def stale() -> bool:
        b = ctx.current_board
        return (ctx.run is not run or run._dead or run.running or run.ends != end
                or getattr(ctx, "shut_down", False) or (b != board0 and b in _NEW_WORK))

    def got(res):
        if run.review_fetch == end:
            run.review_fetch = None
            guard_run_soon(ctx)
        if stale():
            return
        clear_overlay(ctx)
        ctx.start_review(res, run.summary)
        run.record_time()

    def failed(err):
        if run.review_fetch == end:
            run.review_fetch = None
            guard_run_soon(ctx)
        if stale():
            return
        if run.tiles_done:
            open_held_review(ctx, run)
            return
        run._fail_request(err)
    run.result_for_end(got, failed)


def open_held_review(ctx, run: RunController) -> None:
    """The review of the lines the plugin kept from the run's ``tile_done`` events, without the service (a failed
    run's Keep what finished; a result the service no longer has).  When fewer finished tiles reached the plugin than
    TerraByte charged (the connection dropped and the events came back without them), the bar says so."""
    s = getattr(ctx, "review", None)
    if s is not None and not getattr(s, "closed", False) and bool(run.run_id) and getattr(s, "run_id", None) == run.run_id:
        return
    res = run.held_result()
    clear_overlay(ctx)
    ctx.start_review(res, res["summary"])


def partial_text(used: int, held: int) -> str:
    """What is true when the plugin holds the lines of fewer tiles than TerraByte charged: the charge, how many
    tiles' lines are here, and why the rest are gone (said on the card, in the review, in the Recap)."""
    if held <= 0:
        return (f"TerraByte charged {tile_cost_label(used)} for this run. None of their lines are here: they were "
                f"deleted at TerraByte when the run ended, because the plugin could not collect them in time.")
    return (f"TerraByte charged {tile_cost_label(used)} for this run. The lines of {tile_cost_label(held)} are here; "
            f"the rest were deleted at TerraByte when the run ended, because the plugin could not collect them in time.")


def held_short(run) -> bool:
    """The plugin holds the lines of fewer tiles than TerraByte charged, for a run whose end is confirmed."""
    used = None if getattr(run, "charge_pending", False) else run.tiles_used
    return bool(getattr(run, "end_confirmed", False)) and used is not None and run.tiles_held < int(used)


RENEW_WINDOW_ON_RETITLE = False


def retitle(d, title: str) -> None:
    """Give an open dialog a new title and accessible name; with ``RENEW_WINDOW_ON_RETITLE`` its native window is
    also made anew so it carries the title."""
    d.setWindowTitle(title)
    d.setAccessibleName(title)
    if not RENEW_WINDOW_ON_RETITLE or not d.isVisible():
        return
    try:
        pos, wh = d.pos(), d.windowHandle()
        d.hide()
        if wh is not None:
            wh.destroy()
        d.move(pos)
        d.open()
        guard = getattr(getattr(d.parent(), "ctx", None), "click_guard", None)
        if guard is not None:
            guard.stamp(d)
    except RuntimeError:
        pass


class StateBadge(QWidget):
    """20 px state badge of the run lists: done = green-tinted circle with a tick, now = the logo image,
    waiting = outlined ring."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(20, 20)
        self.state = "waiting"
        self._angle = 0
        self._tick = icons.pixmap("check", 11, theme.SUCCESS, 2.4)
        from qgis.PyQt.QtGui import QIcon

        from ...core import paths

        self._logo = QPixmap()
        try:
            self._logo.loadFromData((paths.PLUGIN_DIR / "icon.png").read_bytes())
        except OSError:
            pass
        self._logo = self._logo.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._spin)

    def set_state(self, state: str) -> None:
        self.state = state
        self._timer.stop()
        self.update()

    def _spin(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.75, 0.75, 18.5, 18.5)
        if self.state == "done":
            p.setBrush(QColor(112, 223, 173, 36))
            p.setPen(QPen(QColor(112, 223, 173, 128), 1))
            p.drawEllipse(QRectF(0.5, 0.5, 19, 19))
            p.drawPixmap(QRectF(4.5, 4.5, 11, 11), self._tick, QRectF(0, 0, self._tick.width(), self._tick.height()))
        elif self.state == "now":
            p.drawPixmap(QRectF(0, 0, 20, 20), self._logo, QRectF(0, 0, self._logo.width(), self._logo.height()))
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(247, 245, 251, 64), 1.5))
            p.drawEllipse(r)


class LegendSquare(QWidget):
    """12 px legend squares of EX-Failed, drawn like the grid cells (finished / stopped / not started)."""

    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind
        self.setFixedSize(12, 12)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, 11, 11)
        if self.kind == "finished":
            p.setBrush(QColor(112, 223, 173, 41))
            p.setPen(QPen(QColor(112, 223, 173, 128), 1))
        elif self.kind == "stopped":
            p.setBrush(QColor(245, 158, 11, 26))
            pen = QPen(QColor("#f59e0b"), 1)
            pen.setDashPattern([3, 3])
            p.setPen(pen)
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(247, 245, 251, 51), 1))
        p.drawRoundedRect(r, 2, 2)


class TileSwatch(QWidget):
    """14 px legend squares of EX-Running: finished (amber line in it), decoding (accent frame), queued (dark)."""

    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind
        self.setFixedSize(14, 14)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.kind == "finished":
            p.setBrush(QColor(200, 173, 255, 20))
            p.setPen(QPen(QColor(200, 173, 255, 89), 1))
            p.drawRoundedRect(QRectF(0.5, 0.5, 13, 13), 2, 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.class_colour(1)))
            p.drawRect(QRectF(3, 6, 8, 2))
        elif self.kind == "decoding":
            p.setBrush(QColor(200, 173, 255, 36))
            p.setPen(QPen(QColor(theme.ACCENT), 2))
            p.drawRoundedRect(QRectF(1, 1, 12, 12), 2, 2)
        else:
            p.setBrush(QColor(7, 8, 14, 204))
            p.setPen(QPen(QColor(200, 173, 255, 51), 1))
            p.drawRoundedRect(QRectF(0.5, 0.5, 13, 13), 2, 2)


class _PhaseRow(QWidget):
    def __init__(self, name: str, objname: str):
        super().__init__()
        self.setObjectName(objname)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#%s{border-bottom:1px solid rgba(247,245,251,0.08);}" % objname)
        h = hbox(self, margins=(0, 9, 0, 9), spacing=12)
        self.icon = StateBadge()
        h.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        col = vbox(spacing=2)
        self.name = label(name, name=objname + "Name")
        self.name.setStyleSheet("font-size:14px; font-weight:500;")
        self.detail = label("", muted=True, name=objname + "Detail")
        self.detail.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
        col.addWidget(self.name)
        col.addWidget(self.detail)
        h.addLayout(col, 1)
        self.status = label("", mono=True, wrap=False, name=objname + "Status")
        self.status.setContentsMargins(0, 4, 0, 0)
        h.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)

    def set(self, state: str, detail: str = "") -> None:
        if getattr(self, "_shown", None) == (state, detail):
            return
        self._shown = (state, detail)
        text = {"done": "Done", "now": "Now", "waiting": "Waiting"}[state]
        self.status.setText(text.upper())
        self.status.setProperty("state", state)
        self.icon.set_state(state)
        self.status.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:10px; color:"
                                  f"{theme.SUCCESS if state == 'done' else theme.ACCENT if state == 'now' else theme.TEXT_2};")
        self.name.setStyleSheet(f"font-size:14px; font-weight:500; color:{theme.TEXT_2 if state == 'waiting' else theme.TEXT};")
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))


class _RunScreen(Screen):
    """Common parts of the screens that follow a run."""

    def _connect(self) -> None:
        run = self.ctx.run
        if run is not None and getattr(self, "_conn", None) is not run:
            try:
                if getattr(self, "_conn", None) is not None:
                    self._conn.changed.disconnect(self.refresh)
            except (TypeError, RuntimeError):
                pass
            run.changed.connect(self.refresh)
            self._conn = run

    def refresh(self) -> None:
        pass

    def _stats_row(self) -> QHBoxLayout:
        r = hbox(margins=(0, 4, 0, 0), spacing=16)
        self.elapsed_box, self.elapsed = self._stat("Elapsed", "elapsedStat")
        self.left_box, self.left = self._stat("Left", "leftStat")
        r.addWidget(self.elapsed_box)
        r.addStretch(1)
        r.addWidget(self.left_box)
        return r

    def _stat(self, caption: str, name: str):
        w = QWidget()
        h = hbox(w, spacing=8)
        h.addWidget(_caption(caption))
        v = label("", mono=True, wrap=False, name=name)
        v.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:13px; color:{theme.TEXT};")
        h.addWidget(v)
        return w, v

    def _fill_stats(self, run) -> None:
        _put(self.elapsed, run.elapsed_text())


@registry.screen("EX-Running")
class ExRunning(_RunScreen):
    def __init__(self, ctx):
        super().__init__(ctx)
        lay = _scroll_page(self)
        self.source = LockedSource(ctx)
        lay.addWidget(self.source)
        c = card(name="phaseCard")
        cl = vbox(c, margins=(14, 14, 14, 14), spacing=8)
        cl.addWidget(SectionHeading("Extracting lines"))
        self.p_up = _PhaseRow("Uploading imagery", "phaseUpload")
        self.p_ex = _PhaseRow("Extracting", "phaseExtract")
        for p in (self.p_up, self.p_ex):
            cl.addWidget(p)
        self.bar = QProgressBar()
        self.bar.setObjectName("tilesProgress")
        self.bar.setTextVisible(False)
        cl.addWidget(self.bar)
        cl.addLayout(self._stats_row())
        lay.addWidget(c)
        w = card(name="onMapCard")
        wl = vbox(w, margins=(14, 14, 14, 14), spacing=10)
        wl.addWidget(SectionHeading("On the map"))
        wl.addWidget(label("Tiles fill in from the top-left corner.", name="onMapBody"))
        wl.itemAt(wl.count() - 1).widget().setStyleSheet(f"font-size:13px; color:{theme.TEXT};")
        lg = hbox(spacing=16)
        for txt, kind in (("Finished, with lines", "finished"), ("Working now", "decoding"), ("Waiting", "queued")):
            it = hbox(spacing=7)
            it.addWidget(TileSwatch(kind))
            lb = label(txt, muted=True, wrap=False)
            lb.setStyleSheet(f"font-size:12px; color:{theme.TEXT_2};")
            it.addWidget(lb)
            lg.addLayout(it)
        lg.addStretch(1)
        wl.addLayout(lg)
        lay.addWidget(w)
        lay.addStretch(1)
        self.stop_btn = PillButton("Stop extracting")
        self.stop_btn.setIcon(icons.icon("stop", 14, theme.DANGER))
        self.stop_btn.setIconSize(QSize(14, 14))
        self.stop_btn.setObjectName("stopButton")
        self.stop_btn.setMinimumHeight(48)
        self.stop_btn.setStyleSheet(f"QPushButton{{border:1px solid rgba(239,68,68,0.45); border-radius:24px; background:{theme.CARD_BG};"
                                    f" color:{theme.DANGER}; font-size:14px; font-weight:500;}}")
        self.stop_btn.clicked.connect(self.confirm_stop)
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._tick_elapsed)
        self.pinned.addWidget(self.stop_btn)
        self.stop_dialog: Optional[QDialog] = None
        self.stop_note = label("", muted=True, name="stopNote")
        self.stop_note.hide()
        self.pinned.insertWidget(0, self.stop_note)
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._esc.activated.connect(self.confirm_stop)

    def on_hide(self):
        self._close_stop_dialog()
        self._tick.stop()

    def _tick_elapsed(self) -> None:
        run = self.ctx.run
        if run is not None and run.running and self.stop_dialog is None:
            _put(self.elapsed, run.elapsed_text())

    def on_show(self, **kw):
        self._tick.start()
        run = self.ctx.run
        if run is None:
            return
        self.stop_note.hide()
        self._connect()
        from .map_tools import RunOverlay
        ov = self.ctx.extras.get("overlay")
        if ov is None or ov.run is not run:
            clear_overlay(self.ctx)
            self.ctx.extras["overlay"] = RunOverlay(self.ctx.iface.mapCanvas(), run)
        self.refresh()

    def refresh(self) -> None:
        run = self.ctx.run
        if run is None:
            return
        if self.stop_dialog is not None:
            self._sync_stop_dialog(run)
            return
        self.source.refresh()
        st = run.state
        sending = st == "uploading" or bool(getattr(run, "uploading", False))
        if getattr(run, "checking", False):
            up_detail = f"checking {count(run.checked)} of {tile_cost_label(run.check_total)}"
        elif sending and run.upload_total:
            up_detail = f"{count(run.uploaded)} of {tile_cost_label(run.upload_total)} sent"
        else:
            up_detail = ""
        self.p_up.set("now" if (st in ("idle", "uploading") or sending) else "done", up_detail)
        ex_state = "waiting" if st in ("idle", "uploading") else ("now" if st == "extracting" else "done")
        self.p_ex.set(ex_state, f"{count(run.tiles_done)} of {tile_cost_label(run.tiles_total)}" if ex_state != "waiting" else "")
        if self.bar.maximum() != max(1, run.tiles_total):
            self.bar.setRange(0, max(1, run.tiles_total))
        if self.bar.value() != run.tiles_done:
            self.bar.setValue(run.tiles_done)
        _name(self.bar, f"Tiles extracted, {count(run.tiles_done)} of {count(run.tiles_total)}")
        self._fill_stats(run)
        _show_left(self, run)
        self._sync_stop_dialog(run)

    @staticmethod
    def _stop_body(run=None) -> str:
        return ("Tiles that have already run are charged, and their lines are kept. Tiles that have not run are not "
                "charged. Tiles TerraByte is extracting when you stop may still finish and are charged too.")

    @staticmethod
    def _stopping_body(run=None) -> str:
        return "Waiting for TerraByte to say what it charged for this run."

    @staticmethod
    def _result_body(run) -> str:
        """The service's figure for this run, never the plugin's own count."""
        used = run.tiles_used
        if used is None:
            return ("TerraByte did not say what it charged." +
                    (" The lines found so far are kept." if run.tiles_done else ""))
        if int(used) == 0:
            return "TerraByte charged no tiles for this run."
        refused = getattr(run, "result_refused", None)
        if callable(refused) and refused() and run.tiles_held < int(used):
            return partial_text(int(used), run.tiles_held)
        return f"TerraByte charged {tile_cost_label(int(used))} for this run. Their lines are kept."

    def _stop_state(self) -> str:
        d = self.stop_dialog
        return str(d.property("stopState") or "") if d is not None else ""

    def _close_stop_dialog(self) -> None:
        if getattr(self, "_leave_mode", False):
            self._leave_mode = False
            self.ctx.after_leave = None
            self.ctx.leave_chosen = False
        guard_run_soon(self.ctx)
        d, self.stop_dialog = self.stop_dialog, None
        if d is not None:
            try:
                d.done(0)
                d.deleteLater()
            except RuntimeError:
                pass
            self._refresh_soon()

    def _refresh_soon(self) -> None:
        """The screen stood still behind the Stop dialog: bring it up to date once the dialog is gone."""
        ref = weakref.ref(self)
        QTimer.singleShot(0, lambda: ref() is not None and ref().ctx.current_board == "EX-Running" and ref().refresh())

    def _sync_stop_dialog(self, run) -> None:
        """The run moved on behind the open dialog.  Before Stop: update its counts while it runs, close it once it ended.
        After Stop: when the run ended, show what the service charged (a failure or "no lines" closes it: the card that
        follows says what happened)."""
        d = self.stop_dialog
        if d is None:
            return
        state = self._stop_state()
        lb = d.findChild(QLabel, "stopBody")
        if state == "confirm":
            if not run.running:
                self._close_stop_dialog()
            elif lb is not None:
                lead = LEAVE_LEAD + " " if getattr(self, "_leave_mode", False) else ""
                _put(lb, lead + self._stop_body(run))
        elif state == "stopping":
            if run.running or (run.state in ("stopped", "done") and getattr(run, "charge_pending", False)):
                if lb is not None:
                    lb.setText(self._stopping_body(run))
            elif run.state in ("stopped", "done") or getattr(self, "_leave_mode", False):
                self._show_stop_result(d, run)
            else:
                self._close_stop_dialog()
        elif state == "result" and lb is not None and not getattr(self, "_leave_mode", False) \
                and getattr(self, "_result_run", None) is run and run.result_refused():
            _put(lb, self._result_body(run))

    def _show_stop_result(self, d, run) -> None:
        d.setProperty("stopState", "result")
        self._result_run = run
        guard_run_soon(self.ctx)
        if getattr(self, "_leave_mode", False):
            self._show_leave_result(d, run)
            return
        title = "Run stopped" if run.state == "stopped" else "The run finished first"
        retitle(d, title)
        for name, text in (("stopTitle", title), ("stopBody", self._result_body(run))):
            lb = d.findChild(QLabel, name)
            if lb is not None:
                lb.setText(text)
        for name, show in (("keepRunningButton", False), ("stopConfirmButton", False), ("stopContinueButton", True)):
            b = d.findChild(QPushButton, name)
            if b is not None:
                b.setVisible(show)
                if show:
                    b.setFocus()

    def confirm_leave(self, kind: str = ""):
        """Open the Stop dialog in its leave form ("Keep running" / "Stop the run and leave"); a Stop already under way
        takes the leave form for its result."""
        run = self.ctx.run
        if run is None:
            return None
        d, st = self.stop_dialog, self._stop_state()
        if d is not None and st in ("stopping", "result"):
            self._leave_mode = True
            if st == "result":
                self._show_stop_result(d, run)
            return d
        proceed = getattr(self.ctx, "after_leave", None)
        self._close_stop_dialog()
        self.ctx.after_leave = proceed
        self._leave_mode = True
        return self._open_stop_dialog(run)

    @staticmethod
    def _charge_text(run) -> str:
        used = run.tiles_used
        if used is None:
            return "TerraByte did not say what it charged."
        if int(used) == 0:
            return "TerraByte charged no tiles for this run."
        return f"TerraByte charged {tile_cost_label(int(used))} for this run."

    def _show_leave_result(self, d, run) -> None:
        """After "Stop the run and leave": what the service charged; with finished tiles, Keep what finished (their
        review: the leave is dropped) or Leave without them; TerraByte not reached: Leave or Stay."""
        unreached = run.state not in ("stopped", "done") or bool(getattr(run, "stop_unreachable", False))
        if unreached:
            title, body, show = "The connection dropped", LEAVE_UNREACHED, ("leaveButton", "stayButton")
        else:
            title = "Run stopped" if run.state == "stopped" else "The run finished first"
            lines = run.tiles_done > 0 and run.tiles_used != 0
            short = run.result_refused() and run.tiles_used is not None and run.tiles_held < int(run.tiles_used)
            charge = partial_text(int(run.tiles_used), run.tiles_held) if short else self._charge_text(run)
            body = charge + (" " + LEAVE_KEEP if lines else "")
            show = ("keepFinishedButton", "leaveButton") if lines else ("leaveButton",)
        retitle(d, title)
        for name, text in (("stopTitle", title), ("stopBody", body)):
            lb = d.findChild(QLabel, name)
            if lb is not None:
                lb.setText(text)
        leave = d.findChild(QPushButton, "leaveButton")
        if leave is not None:
            leave.setText(LEAVE_WITHOUT if "keepFinishedButton" in show else LEAVE)
        for name in ("keepRunningButton", "stopConfirmButton", "stopContinueButton", "keepFinishedButton",
                     "leaveButton", "stayButton"):
            b = d.findChild(QPushButton, name)
            if b is not None:
                b.setVisible(name in show)
        first = d.findChild(QPushButton, show[0])
        if first is not None:
            first.setFocus()

    def _keep_after_leave(self) -> None:
        """Keep what finished / Stay: the leave is dropped; the run's end goes on as after Stop (its review, or its
        card)."""
        self.ctx.after_leave = None
        self._continue_after_stop()

    def _leave_now(self) -> None:
        """Leave (without them): the run is stopped at TerraByte; QGIS goes on with what was asked (close, quit, open,
        new): its own questions come now, after the plugin's."""
        ctx = self.ctx
        proceed, ctx.after_leave = getattr(ctx, "after_leave", None), None
        ctx.leave_chosen = True
        run, self._result_run = getattr(self, "_result_run", None) or ctx.run, None
        self._leave_mode = False
        self._close_stop_dialog()
        if run is not None:
            run.end_taken = run.ends
        clear_overlay(ctx)
        ctx.go("EX-Start")
        if proceed is not None:
            QTimer.singleShot(0, proceed)

    def holds_run_end(self, run) -> bool:
        """True while the Stop dialog shows this run's charge, or waits for it at the end of the stop: the flow goes
        on when it is closed (``finished``)."""
        st = self._stop_state()
        if st == "result":
            return getattr(self, "_result_run", None) is run
        return (st == "stopping" and getattr(self, "_stopping_run", None) is run and run.state in ("stopped", "done")
                and bool(getattr(run, "charge_pending", False)))

    def _continue_after_stop(self) -> None:
        run, self._result_run = getattr(self, "_result_run", None), None
        if self._stop_state() == "result":
            self._close_stop_dialog()
        ctx = self.ctx
        if run is None or ctx.run is not run or ctx.current_board != "EX-Running":
            return
        after_run_end(ctx, run, run.state)

    def do_stop(self) -> bool:
        """Stop for real: ask the service to stop, then wait in the dialog for what it charged.  A run that already
        finished is left alone and the panel says so."""
        run = self.ctx.run
        if run is None or not run.stop():
            self._close_stop_dialog()
            self.stop_note.setText("This run already finished. Nothing to stop.")
            self.stop_note.show()
            return False
        d = self.stop_dialog
        if d is None:
            d = self._open_stop_dialog(run)
        d.setProperty("stopState", "stopping")
        self._stopping_run = run
        retitle(d, "Stopping\u2026")
        for name, text in (("stopTitle", "Stopping\u2026"), ("stopBody", self._stopping_body(run))):
            lb = d.findChild(QLabel, name)
            if lb is not None:
                lb.setText(text)
        for name in ("keepRunningButton", "stopConfirmButton", "stopContinueButton", "keepFinishedButton",
                     "leaveButton", "stayButton"):
            b = d.findChild(QPushButton, name)
            if b is not None:
                b.setVisible(False)
        self._sync_stop_dialog(run)
        return True

    def confirm_stop(self) -> None:
        t0 = time.perf_counter()
        try:
            self._confirm_stop()
        finally:
            took = (time.perf_counter() - t0) * 1000.0
            d = self.stop_dialog
            if d is not None:
                d._t_click = t0
                log(f"Stop dialog: the click took {took:.0f} ms here", Qgis.MessageLevel.Info)

    def _confirm_stop(self) -> None:
        run = self.ctx.run
        if self.ctx.current_board != "EX-Running":
            return
        if self._stop_state() in ("stopping", "result"):
            return
        if run is None or not run.running:
            self.stop_note.setText("This run already finished. Nothing to stop.")
            self.stop_note.show()
            return
        self._close_stop_dialog()
        self._open_stop_dialog(run)

    def _open_stop_dialog(self, run) -> QDialog:
        leaving = bool(getattr(self, "_leave_mode", False))
        d = QDialog(self)
        d.setObjectName("stopDialog")
        d.setProperty("stopState", "confirm")
        d.setWindowTitle("Stop extracting?")
        d.setAccessibleName("Stop extracting?")
        card_dialog(d)
        d.setStyleSheet(theme.base_qss() + f"QDialog#stopDialog{{background:{theme.BG_DIALOG};"
                        f"border:1px solid {theme.BORDER_STRONG};border-radius:{theme.R_DIALOG}px;}}")
        lay = vbox(d, margins=(20, 18, 20, 18), spacing=12)
        t = label("Stop extracting?", h2=True, name="stopTitle")
        lay.addWidget(t)
        lay.addWidget(label((LEAVE_LEAD + " " if leaving else "") + self._stop_body(run), name="stopBody"))
        row = hbox(spacing=8)
        row.addStretch(1)
        keep = button("Keep running", ghost=True, name="keepRunningButton")
        stop = button(LEAVE_STOP if leaving else "Stop", destructive=True, name="stopConfirmButton")
        cont = button("Continue", primary=True, name="stopContinueButton")
        stay = button(STAY, ghost=True, name="stayButton")
        leave = button(LEAVE, destructive=True, name="leaveButton")
        kept = button("Keep what finished", primary=True, name="keepFinishedButton")
        for b in (cont, stay, leave, kept):
            b.hide()
        keep.clicked.connect(d.reject)
        stop.clicked.connect(self.do_stop)
        cont.clicked.connect(self._continue_after_stop)
        stay.clicked.connect(self._keep_after_leave)
        kept.clicked.connect(self._keep_after_leave)
        leave.clicked.connect(self._leave_now)
        if leaving:
            keep.setDefault(True)
        row.addWidget(keep)
        row.addWidget(stop)
        row.addWidget(stay)
        row.addWidget(leave)
        row.addWidget(kept)
        row.addWidget(cont)
        lay.addLayout(row)
        self.stop_dialog = d

        def closed(_r, dd=d):
            if self.stop_dialog is dd:
                self._refresh_soon()
                st = self._stop_state()
                if getattr(self, "_leave_mode", False):
                    self._leave_mode = False
                    self.ctx.after_leave = None
                    self.ctx.leave_chosen = False
                held = st == "stopping" and self.holds_run_end(self.ctx.run) if self.ctx.run is not None else False
                result = st == "result"
                self.stop_dialog = None
                if held:
                    self._result_run = self.ctx.run
                    result = True
                if result:
                    QTimer.singleShot(0, self._continue_after_stop)
        d.finished.connect(closed)
        d.open()
        guard = getattr(self.ctx, "click_guard", None)
        if guard is not None:
            guard.stamp(d)
        return d


class TileGrid(QWidget):
    """Where it stopped: finished / stopped / not-started tiles (EX-Failed)."""

    def __init__(self):
        super().__init__()
        self.rows = self.cols = 0
        self.cells = {}
        self.first = None
        self.setFixedSize(216, 216)
        self.setObjectName("tileGrid")

    def set_run(self, run) -> None:
        a = run.area
        self.rows, self.cols = a.rows, a.cols
        self.cells = {}
        for rc in run.order:
            self.cells[rc] = ("finished" if rc in run.done_tiles else
                              "stopped" if (rc in run.failed_tiles or rc in run.started_tiles) else "none")
        nxt = [rc for rc in run.order if rc not in run.done_tiles]
        self.first = nxt[0] if nxt else None
        self.update()

    def counts(self):
        out = {"finished": 0, "stopped": 0, "none": 0}
        for v in self.cells.values():
            out[v] += 1
        return out

    def paintEvent(self, _e):
        if not self.rows or not self.cols:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pitch = 216.0 / max(self.rows, self.cols)
        side = pitch - 2
        for (r, c), st in self.cells.items():
            rect = QRectF(1 + c * pitch, 1 + r * pitch, side, side)
            if st == "finished":
                p.setBrush(QColor(112, 223, 173, 41))
                p.setPen(QPen(QColor(112, 223, 173, 128), 1))
            elif st == "stopped":
                p.setBrush(QColor(245, 158, 11, 26))
                pen = QPen(QColor("#f59e0b"), 1)
                pen.setDashPattern([3, 3])
                p.setPen(pen)
            else:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(247, 245, 251, 36), 1))
            p.drawRoundedRect(rect, 2, 2)
            if self.first == (r, c) and st == "stopped":
                p.setPen(QColor("#f59e0b"))
                f = p.font()
                f.setFamily("IBM Plex Mono")
                f.setPixelSize(11)
                p.setFont(f)
                p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), str(self.parent_no))
        p.end()

    parent_no = 0


LEAVE_LEAD = "A run is going on. To leave, stop it first."
LEAVE_STOP = "Stop the run and leave"
LEAVE_KEEP = "Keep what finished to see their lines, or leave without them."
LEAVE_WITHOUT = "Leave without them"
LEAVE = "Leave"
STAY = "Stay"
LEAVE_UNREACHED = "TerraByte could not be reached to stop the run, so it may still be going on there."
RESERVED_BACK = "The tiles held for it come back as soon as TerraByte can be reached."


def guard_run_soon(ctx) -> None:
    """The leave guard looks again, after this turn of the event loop, at whether the run is at stake (in flight,
    its Stop dialog showing or waiting for its charge, or its lines on their way to the review)."""
    ref = weakref.ref(ctx)

    def look() -> None:
        c = ref()
        lg = getattr(c, "leave_guard", None) if c is not None else None
        if lg is not None:
            try:
                lg.refresh_run()
            except RuntimeError:
                pass
    QTimer.singleShot(0, look)


def guard_card_soon(ctx) -> None:
    """The leave guard looks again, after this turn of the event loop, at whether the run's card holds the only
    copy of charged lines."""
    ref = weakref.ref(ctx)

    def look() -> None:
        c = ref()
        lg = getattr(c, "leave_guard", None) if c is not None else None
        if lg is not None:
            try:
                lg.refresh_card()
            except RuntimeError:
                pass
    QTimer.singleShot(0, look)


def ask_leave_card(ctx, kind: str = ""):
    """The leave guard's question on the run's card (its lines are kept only here).  None without the card."""
    panel = getattr(ctx, "panel", None)
    s = getattr(panel, "_screens", {}).get("EX-Failed") if panel is not None else None
    return s.confirm_leave(kind) if s is not None and ctx.current_board == "EX-Failed" else None


def ask_leave_run(ctx, kind: str = ""):
    """The leave guard's question for a run in flight, on the Running screen (its Stop dialog, in its leave form; a
    Stop dialog already showing or waiting for the charge takes the leave form).  None when no run is in flight or
    there is no panel."""
    run = getattr(ctx, "run", None)
    panel = getattr(ctx, "panel", None)
    if run is None or panel is None:
        return None
    shown = getattr(panel, "_screens", {}).get("EX-Running")
    held = shown is not None and shown.holds_run_end(run)
    if not (run.running or getattr(run, "checking", False) or held):
        return None
    if ctx.current_board != "EX-Running":
        ctx.go("EX-Running")
    s = ctx.panel.screen("EX-Running") if ctx.current_board == "EX-Running" else None
    return s.confirm_leave(kind) if s is not None else None


STOPPING_THERE = "Stopping the run at TerraByte first\u2026"
UNREACHABLE_TRY_AGAIN = "Couldn't reach TerraByte. Check your connection, then try again."
GONE_ON_TEXT = ("Keep what finished stops it and shows the lines of the tiles that finished, and Run the area again stops "
                "it and runs the area anew.")
AGAIN_CHARGED_TEXT = "Run the area again charges its tiles again."
STATUS_RETRY_S = 10.0
STATUS_TRIES = 60
CARD_LEAVE_TITLE = "Keep what finished?"


def card_leave_text(run) -> str:
    """The card's leave question: the charge, and that the lines are kept only here."""
    ended = run is not None and bool(getattr(run, "end_confirmed", False))
    used = run.tiles_used if ended and not getattr(run, "charge_pending", False) else None
    charge = f"TerraByte charged {tile_cost_label(int(used))} for this run. " if used else ""
    kept = ("The lines of its finished tiles are kept only here: TerraByte deleted its copy when the run ended. "
            if ended else "The lines of the tiles that finished so far are kept only here. ")
    return charge + kept + "Keep what finished to see them, or leave without them."


def ended_text(run) -> str:
    """After "The run stopped at …": what TerraByte charged, and that the lines of the finished tiles are the ones
    the plugin kept (or how many of them reached it); no promise that TerraByte keeps anything."""
    used = None if getattr(run, "charge_pending", False) else run.tiles_used
    held = run.tiles_held
    if used is not None and int(used) > 0 and held < int(used):
        tail = " Keep what finished shows those." if held else ""
        return partial_text(int(used), held) + tail + " A run that ended can't be resumed."
    if used is None:
        charge = "TerraByte charges only the tiles that finished."
    elif int(used) == 0:
        charge = "TerraByte charged no tiles for this run."
    else:
        charge = f"TerraByte charged {tile_cost_label(int(used))} for this run: the tiles that finished."
    if held == 0:
        lines = " No lines reached the plugin, so there are none to keep."
    else:
        lines = " Their lines are the ones kept here, and Keep what finished shows them."
    return charge + lines + " A run that ended can't be resumed."


def _stream_wait_text(ctx) -> str:
    """'2 minutes': how long a streaming run waits for a tile before it ends by itself (``meta.stream.wait_s``;
    120 s when the service sends none)."""
    spec = stream_spec(ctx) or {}
    try:
        s = int(float(spec.get("wait_s", 120)))
    except (TypeError, ValueError):
        s = 120
    m = max(1, round(s / 60))
    return f"{s} seconds" if s < 60 else ("1 minute" if m == 1 else f"{m} minutes")


@registry.screen("EX-Failed")
class ExFailed(_RunScreen):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.kind = "connection"
        lay = _scroll_page(self)
        self.source = LockedSource(ctx)
        lay.addWidget(self.source)
        self.alert = QFrame()
        self.alert.setObjectName("failAlert")
        self.alert.setStyleSheet("QFrame#failAlert{border:1px solid rgba(245,158,11,0.4);"
                                 " background:rgba(245,158,11,0.10); border-radius:12px;}")
        al = vbox(self.alert, margins=(14, 14, 14, 14), spacing=10)
        hr = hbox(spacing=10)
        self.heading_icon = QLabel()
        self.heading_icon.setStyleSheet("background:transparent; border:none;")
        hr.addWidget(self.heading_icon)
        self.heading = label("", name="failHeading")
        self.heading.setStyleSheet("font-size:16px; font-weight:500;")
        hr.addWidget(self.heading, 1)
        al.addLayout(hr)
        self.body = label("", name="failBody")
        al.addWidget(self.body)
        lay.addWidget(self.alert)
        self.where = card(name="whereStoppedCard")
        wl = vbox(self.where, margins=(14, 14, 14, 14), spacing=12)
        wl.addWidget(SectionHeading("Where it stopped"))
        row = hbox(spacing=16)
        self.tgrid = TileGrid()
        row.addWidget(self.tgrid)
        leg = vbox(spacing=10)
        leg.addStretch(1)
        self.legend = {}
        for key, col in (("finished", "#70dfad"), ("stopped", "#f59e0b"), ("none", "#555560")):
            r = hbox(spacing=8)
            r.addWidget(LegendSquare(key))
            lb = label("", muted=True, wrap=False, name=f"legend_{key}")
            r.addWidget(lb)
            self.legend[key] = lb
            leg.addLayout(r)
        leg.addStretch(1)
        row.addLayout(leg, 1)
        wl.addLayout(row)
        self.facts = []
        for i, (ic, col) in enumerate((("check", theme.SUCCESS), ("retry-arrow", theme.ACCENT),
                                       ("check-circle", theme.SUCCESS))):
            r = hbox(spacing=10)
            r.addWidget(_svg(ic, 14, col, 1.8, top=1), 0, Qt.AlignmentFlag.AlignTop)
            lb = label("", name=f"fact{i + 1}")
            lb.setStyleSheet("font-size:13px;")
            r.addWidget(lb, 1)
            self.facts.append(lb)
            wl.addLayout(r)
        lay.addWidget(self.where)
        self.hint = label("", muted=True, name="failHint")
        self.hint.setStyleSheet("font-size:12px;")
        self.hint.setTextFormat(Qt.TextFormat.RichText)
        self.hint.setOpenExternalLinks(False)
        pal = self.hint.palette()
        pal.setColor(QPalette.ColorRole.Link, QColor(theme.ACCENT))
        self.hint.setPalette(pal)
        self.hint.linkActivated.connect(lambda _h: ctx.open_url(HELP_MAILTO))
        self.hint.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.hint)
        lay.addStretch(1)
        self.primary = ArrowButton("", arrow="rotate-cw")
        self.primary.setObjectName("primaryButton")
        self.secondary = pill_button("", name="secondaryButton")
        self.note = label("", muted=True, name="failNote")
        self.note.hide()
        self.pinned.addWidget(self.note)
        self.pinned.addWidget(self.primary)
        self.pinned.addWidget(self.secondary)
        self._p_slot = None
        self._s_slot = None

    def on_hide(self) -> None:
        d, self.leave_dialog = getattr(self, "leave_dialog", None), None
        if d is not None:
            try:
                d.reject()
            except RuntimeError:
                pass
        guard_card_soon(self.ctx)

    def confirm_leave(self, kind: str = "") -> QDialog:
        """"Keep what finished?": Keep what finished (their review; the user stays), Leave without them (QGIS goes on
        with what was asked), or Stay.  Esc or the x is Stay."""
        old = getattr(self, "leave_dialog", None)
        if old is not None and old.isVisible():
            return old
        run = self.ctx.run
        d = QDialog(self)
        d.setObjectName("leaveCardDialog")
        d.setWindowTitle(CARD_LEAVE_TITLE)
        d.setAccessibleName(CARD_LEAVE_TITLE)
        card_dialog(d)
        d.setStyleSheet(theme.base_qss() + f"QDialog#leaveCardDialog{{background:{theme.BG_DIALOG};"
                        f"border:1px solid {theme.BORDER_STRONG};border-radius:{theme.R_DIALOG}px;}}")
        lay = vbox(d, margins=(20, 18, 20, 18), spacing=12)
        lay.addWidget(label(CARD_LEAVE_TITLE, h2=True, name="leaveCardTitle"))
        lay.addWidget(label(card_leave_text(run), name="leaveCardBody"))
        row = hbox(spacing=8)
        row.addStretch(1)
        stay = button(STAY, ghost=True, name="stayButton")
        leave = button(LEAVE_WITHOUT, destructive=True, name="leaveButton")
        kept = button("Keep what finished", primary=True, name="keepFinishedButton")
        kept.setDefault(True)
        stay.clicked.connect(d.reject)
        leave.clicked.connect(self._leave_without_them)
        kept.clicked.connect(self._keep_after_leave)
        for b in (stay, leave, kept):
            row.addWidget(b)
        lay.addLayout(row)
        self.leave_dialog = d

        def closed(_r, dd=d) -> None:
            if getattr(self, "leave_dialog", None) is dd:
                self.leave_dialog = None
            if not getattr(dd, "_left", False):
                self.ctx.after_leave = None
                self.ctx.leave_chosen = False
        d.finished.connect(closed)
        d.open()
        guard = getattr(self.ctx, "click_guard", None)
        if guard is not None:
            guard.stamp(d)
        return d

    def _keep_after_leave(self) -> None:
        d = getattr(self, "leave_dialog", None)
        if d is not None:
            d.reject()
        self.review_finished()

    def _leave_without_them(self) -> None:
        """The user leaves without the lines: the run is let go (its lines with it) and QGIS goes on."""
        ctx = self.ctx
        proceed, ctx.after_leave = getattr(ctx, "after_leave", None), None
        ctx.leave_chosen = True
        d = getattr(self, "leave_dialog", None)
        if d is not None:
            d._left = True
            d.accept()
        run = ctx.run
        if run is not None:
            run.dispose()
            ctx.run = None
        clear_overlay(ctx)
        ctx.go("EX-Start")
        if proceed is not None:
            QTimer.singleShot(0, proceed)

    def on_show(self, kind: str = "connection", **kw):
        self.kind = kind
        self.note.hide()
        self._busy = False
        clear_overlay(self.ctx)
        self._connect()
        run = self.ctx.run
        self._dropped = bool(kind == "connection" and run is not None and not getattr(run, "end_confirmed", True))
        self.refresh()
        if self._dropped:
            self._watch_status(run)

    def _watch_status(self, run, n: int = 0) -> None:
        """While "The connection dropped" is up and the run's end is not read: one ``GET /v1/runs/{id}`` every
        STATUS_RETRY_S (at most STATUS_TRIES in all).  Still going on there: the card says so, and the run is read
        again later.  Ended there: its events are read to that end at once (no Stop), so the lines of tiles that
        finished meanwhile are kept while TerraByte still has them, and the card then says the charge and how many
        tiles' lines are here; that ends the watch.  A 4xx answer (a 410 included) ends it too: nothing is asked
        again."""
        ctx = self.ctx
        if n == 0:
            self._watching = run
        if (getattr(self, "_watching", None) is not run or ctx.run is not run or run._dead
                or ctx.current_board != "EX-Failed" or getattr(run, "end_confirmed", True) or self._busy):
            return
        ref = weakref.ref(self)

        def ok(res) -> None:
            me = ref()
            if me is None or getattr(me, "_watching", None) is not run or run._dead:
                return
            state = str((res or {}).get("state") or "")
            if state in ("done", "stopped", "failed", "nothing_found"):
                me._watching = None
                run.follow_end(lambda: me._status_read(run), lambda: me._status_read(run))
                return
            run.going_on_there = True
            me._status_read(run)
            if n + 1 < STATUS_TRIES:
                QTimer.singleShot(int(STATUS_RETRY_S * 1000),
                                  lambda: ref() is not None and ref()._watch_status(run, n + 1))
            else:
                me._watching = None

        def err(e) -> None:
            me = ref()
            if me is None or getattr(me, "_watching", None) is not run:
                return
            status = int(getattr(e, "status", 0) or 0)
            if (status == 0 or status == 429 or status >= 500) and n + 1 < STATUS_TRIES:
                QTimer.singleShot(int(STATUS_RETRY_S * 1000), lambda: ref() is not None and ref()._watch_status(run, n + 1))
            else:
                me._watching = None
        if n == 0:
            QTimer.singleShot(int(STATUS_RETRY_S * 1000), lambda: ref() is not None and ref()._status_first(run))
            return
        run.read_status(ok, err)

    def _status_first(self, run) -> None:
        if getattr(self, "_watching", None) is run:
            self._watch_status(run, 1)

    def _status_read(self, run) -> None:
        if self.ctx.run is run and self.ctx.current_board == "EX-Failed":
            self.refresh()

    def _bind(self, btn, fn) -> None:
        try:
            btn.clicked.disconnect()
        except (TypeError, RuntimeError):
            pass
        btn.clicked.connect(fn)

    def refresh(self) -> None:
        run = self.ctx.run
        if run is None:
            return
        self.source.refresh()
        kind = self.kind
        n = run.next_tile_no
        self.shown_kind = kind
        self.where.setVisible(kind in ("connection", "stopped"))
        self.hint.setVisible(True)
        el = run.elapsed_text()
        link = f"<a href='mailto' style='color:{theme.ACCENT}; text-decoration:none'>Email {SUPPORT_EMAIL}</a>"
        lead = "Check your connection, then try again." if kind == "connection" else ""
        self.hint.setText(f"{lead} Still stuck? {link}".strip())
        ended = getattr(run, "end_confirmed", True) or getattr(run, "stop_answered", False)
        if kind == "connection" and getattr(run, "stop_unreachable", False) and getattr(run, "streaming", False):
            self.heading.setText("The connection dropped")
            self.body.setText(f"Your connection to TerraByte dropped at {el}. TerraByte could not be reached to stop "
                              f"the run: it stops by itself about {_stream_wait_text(self.ctx)} after the last tile it "
                              f"got, and charges only the tiles it finished.")
        elif kind == "connection" and not ended:
            unreached = ("TerraByte could not be reached to stop the run, so it may still be going on there"
                         if getattr(run, "stop_unreachable", False) else "The run may still be going on there")
            if getattr(run, "going_on_there", False):
                unreached = "TerraByte can be reached again, and the run is still going on there"
            self.heading.setText("The connection dropped")
            self.body.setText(f"Your connection to TerraByte dropped at {el}. {unreached}: {GONE_ON_TEXT}")
        elif kind == "connection" and getattr(self, "_dropped", False):
            self.heading.setText("The connection dropped")
            self.body.setText(f"Your connection to TerraByte dropped at {el}, and the run ended there meanwhile. "
                              f"{ended_text(run)}")
        elif kind in ("connection", "stopped"):
            self.heading.setText("The connection dropped" if kind == "connection" else "The run stopped")
            self.body.setText(f"The run stopped at {el}. {ended_text(run)}")
        elif kind == "nothing":
            n_used = int(self.ctx.extras.get("nothing_tiles", 0) or 0)
            self.heading.setText("No lines found in this area")
            self.body.setText((f"{tile_cost_label(n_used)} {'was' if n_used == 1 else 'were'} charged. " if n_used else "")
                              + "The imagery may be too soft to read lines from, or the area has none.")
        elif kind == "deleted":
            err = run.problem_error
            self.heading.setText(DELETED_HEADING)
            self.body.setText(_clean(err.message if err else "") or DELETED_TEXT)
        elif kind == "busy":
            self.heading.setText("TerraByte is busy right now")
            self.body.setText("Nothing was started and no tiles were used. Try again in a few minutes.")
        else:
            self.heading.setText("Couldn't start the run")
            err = run.problem_error
            from ...cloud.runner import RELEASING

            held = " " + RESERVED_BACK if run.run_id and run.run_id in RELEASING else ""
            self.body.setText(_clean(err.message if err else "") + " Nothing was started and no tiles were used." + held)
        col = theme.WARNING
        icons.set_label_icon(self.heading_icon, "wifi-off" if kind == "connection" else "alert-triangle",
                             18, col, 1.8)
        self.alert.setStyleSheet(f"QFrame#failAlert{{border:1px solid rgba(245,158,11,0.4);"
                                 f" background:rgba(245,158,11,0.10); border-radius:12px;}}")
        self.tgrid.parent_no = n
        self.tgrid.set_run(run)
        self.tgrid.setAccessibleName(
            f"{run.area.rows} by {run.area.cols} tile grid. {tile_cost_label(self.tgrid.counts()['finished'])} finished from the "
            f"top-left, {tile_cost_label(self.tgrid.counts()['stopped'])} stopped part way, {count(self.tgrid.counts()['none'])} not started.")
        c = self.tgrid.counts()
        self.legend["finished"].setText(f"{count(c['finished'])} finished")
        self.legend["stopped"].setText(f"{count(c['stopped'])} stopped")
        self.legend["none"].setText(f"{count(c['none'])} not started")
        if held_short(run):
            self.facts[0].setText(f"{count(run.tiles_held)} of {tile_cost_label(int(run.tiles_used))} charged have "
                                  f"their lines here.")
        else:
            self.facts[0].setText(f"{count(c['finished'])} of {tile_cost_label(run.tiles_total)} finished.")
        self.facts[1].setText(AGAIN_CHARGED_TEXT)
        self.facts[2].setText("Failed tiles are not charged.")
        ctx = self.ctx
        if kind in ("connection", "stopped"):
            self.primary.setText(RUN_AGAIN_TEXT)
            self.primary.setEnabled(True)
            self._bind(self.primary, lambda: self._first_its_end(self.run_again))
            self.secondary.setText("Keep what finished")
            self.secondary.setEnabled(run.tiles_done > 0 or not ended)
            self._bind(self.secondary, self.review_finished)
        elif kind == "deleted":
            self.primary.setText(RUN_AGAIN_TEXT)
            self.primary.setEnabled(True)
            self._bind(self.primary, self.run_again)
            self.secondary.setText("New area")
            self.secondary.setEnabled(True)
            self._bind(self.secondary, lambda: self._fresh_area("EX-DrawArea"))
        elif kind == "nothing":
            self.primary.setText("New area")
            self.primary.setEnabled(True)
            self._bind(self.primary, lambda: self._fresh_area("EX-DrawArea"))
            self.secondary.setText("Pick other imagery")
            self.secondary.setEnabled(True)
            self._bind(self.secondary, lambda: self._fresh_area("EX-Start"))
        else:
            self.primary.setText("Try again")
            self.primary.setEnabled(True)
            self._bind(self.primary, self.try_again)
            self.secondary.setText("Exit")
            self.secondary.setEnabled(True)
            self._bind(self.secondary, lambda: (self._let_go_of_run(), ctx.go("EX-Start")))
        if getattr(self, "_busy", False):
            self.primary.setEnabled(False)
            self.secondary.setEnabled(False)
        guard_card_soon(ctx)

    def run_again(self) -> None:
        """The run's data is deleted at its end, so it can't be resumed: the run check opens on the same area (its
        tiles are charged again when it runs)."""
        ctx = self.ctx
        run = ctx.run
        if run is None or ctx.current_board != "EX-Failed":
            return
        area = run.area
        run.dispose()
        ctx.run = None
        clear_overlay(ctx)
        if not imagery_in_project(ctx):
            refuse_stale_imagery(ctx)
            return
        ctx.area = area
        ctx.go("EX-Setup")

    def _fresh_area(self, board: str) -> None:
        """After a run with no lines, New area starts empty (no earlier rectangle, Next disabled)."""
        ctx = self.ctx
        ctx.area = None
        clear_overlay(ctx)
        ctx.go(board)

    def _say(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def _first_its_end(self, then) -> None:
        """The run's end was the plugin's (a lost connection, a Stop that did not get through), so it may still be
        running at TerraByte: stop it there and follow it to its end and charge, then ``then()``.  A run whose end and
        charge are known goes straight to ``then()``."""
        run, ctx = self.ctx.run, self.ctx
        if run is None:
            return
        if getattr(run, "end_confirmed", True) and not getattr(run, "charge_pending", False):
            then()
            return
        self._watching = None
        self._say(STOPPING_THERE)
        self._busy = True
        self.primary.setEnabled(False)
        self.secondary.setEnabled(False)

        def ok() -> None:
            self._busy = False
            if ctx.run is run and ctx.current_board == "EX-Failed":
                self._say("")
                self.refresh()
                then()

        def err() -> None:
            self._busy = False
            if ctx.run is run and ctx.current_board == "EX-Failed":
                self.refresh()
                self._say(UNREACHABLE_TRY_AGAIN)
        run.ensure_ended(ok, err)

    def review_finished(self) -> None:
        """Keep what finished: the review of the lines the plugin kept as the tiles finished; the service deleted its
        copy when the run ended.  A run that may still go on there is stopped first."""
        run, ctx = self.ctx.run, self.ctx
        if run is None:
            return

        def keep() -> None:
            if not run.tiles_done:
                self._say("No tiles finished in this run: there are no lines to keep.")
                return
            open_held_review(ctx, run)
        self._first_its_end(keep)

    def _let_go_of_run(self) -> None:
        """A run that failed before it ran (an upload that failed for good, a start refused) is left: stop it at
        TerraByte, so its reserved tiles come back at once."""
        run = self.ctx.run
        if run is None or not run.run_id or self.shown_kind != "error" or run.tiles_done:
            return
        rid = run.run_id
        try:
            from ...cloud.runner import release_reservation

            release_reservation(self.ctx, rid, run)
        except Exception as e:
            log(f"Left run {rid}: Stop not sent ({type(e).__name__})", Qgis.MessageLevel.Warning)

    def try_again(self) -> None:
        run = self.ctx.run
        if run is None or not run_gate(run):
            return
        if self.shown_kind == "error":
            self._let_go_of_run()
            run.run_id = None
        self.ctx.go("EX-Running")
        run.retry_start()
