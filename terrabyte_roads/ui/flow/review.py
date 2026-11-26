"""Review screens: RV-Keep, RV-Export, RV-Exit (dialog), RV-Recap.

Screens read and drive the ``ReviewSession`` held in ``ctx.review`` (see review_session.py).
"""
from __future__ import annotations

import os
import re
import sys
import traceback
import weakref
from typing import Any, Dict, List, Optional

from qgis.core import Qgis, QgsProject, QgsRasterLayer
from qgis.PyQt.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QGuiApplication, QPainter, QPen, QValidator
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QGraphicsOpacityEffect, QLabel, QLineEdit,
                                 QFileDialog, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QWidget)

from ...core import export as export_mod
from .. import icons, theme
from .overlay_hooks import MessageBarHook
from ..widgets import (FitScrollArea, SCROLLBAR_QSS, style_combo_popup, card_dialog, class_swatch, message_bar_inset, Meter, MonoNumberLabel, Screen, SectionHeading, Swatch, button, card, hairline, hbox, label,
                       name_combo, set_tip, track, vbox)
from . import registry
from .review_session import DEFAULT_OFF, ReviewSession
from ...core.numbers import count, counted
from ...cloud.grid import km2_label

MONO = theme.FONT_MONO


FAINT = QColor(247, 245, 251, 36)
MID = QColor(247, 245, 251, 77)


def _fmt_km(v: float) -> str:
    """A length in km for a column headed km: ``1.2``, and ``32 m`` below 0.1 km."""
    if v <= 0:
        return "0"
    return f"{max(1, round(v * 1000))} m" if v < 0.1 else f"{v:.1f}"


def length_text(km: float) -> str:
    """``1.2 km``, and ``32 m`` below 0.1 km, so a short line never reads ``0.0 km``."""
    if km <= 0:
        return "0 km"
    return f"{max(1, round(km * 1000))} m" if km < 0.1 else f"{km:.1f} km"


def _tiles_caption(s) -> str:
    """``From 36 tiles``; after a stopped or failed run ``3 tiles charged`` (what the service billed).  When fewer
    tiles' lines are here than were charged, ``From 24 tiles`` (those tiles), and the note says the charge."""
    return f"From {s.tiles_words()}" if getattr(s, "partial", False) or not s.stopped else s.tiles_words()


def held_note(s) -> str:
    """Keep's note when the review holds the lines of fewer tiles than were charged."""
    from .extract import partial_text

    return partial_text(int(s.tiles_charged), int(s.tiles_held or 0))


def _plural(n: int, one: str, many: str) -> str:
    return counted(n, one, many)


def stopped_note(used: int, planned: int) -> str:
    """After a stopped or failed run, on Keep and the Recap: the service's charge of the tiles planned, never the
    plugin's own count."""
    return (f"The run stopped early: {count(used)} of {_plural(planned, 'tile', 'tiles')} charged." if planned >= used > 0
            else f"The run stopped early: {_plural(used, 'tile', 'tiles')} charged.")


MIN_TIP = "Drop lines shorter than this. 0 keeps every line."
MIN_LEN_MAX_M = 10000.0
MIN_LEN_PATTERN = r"^\d{0,5}([.,]\d{0,2})?$"
MIN_LEN_ERROR = f"Only 0 to {count(MIN_LEN_MAX_M)} m is taken."


def parse_min_len(text: str) -> Optional[float]:
    """The metres typed in the minimum-length field, or None when it is not a length from 0 to MIN_LEN_MAX_M
    (an empty field is 0: every line kept)."""
    t = (text or "").strip().replace(",", ".")
    if not t:
        return 0.0
    if not re.fullmatch(r"\d+(\.\d*)?|\.\d+", t):
        return None
    v = float(t)
    return v if 0.0 <= v <= MIN_LEN_MAX_M else None


def min_len_text(m: float) -> str:
    """'25', '1.5': the minimum length as the field shows it."""
    return f"{m:g}"


def is_min_len_text(text: str) -> bool:
    """A text the minimum-length field may show: a length from 0 to MIN_LEN_MAX_M with at most two decimals
    (empty is 0, shown as a grey 0).  Every such text is the length the filter uses; no other text is ever shown."""
    return re.fullmatch(MIN_LEN_PATTERN, text or "") is not None and parse_min_len(text) is not None


class MinLenValidator(QValidator):
    """The minimum-length field takes a key, a paste or a value set from outside (an accessibility tool's
    "set value") only when the whole text it makes is a length from 0 to MIN_LEN_MAX_M (``is_min_len_text``); anything
    else is refused whole (Invalid, never Intermediate: "-", "abc", the fifth 9 of 99999, "10001", "."), so the field
    never shows a text the filter does not use.  ``on_refused`` is called on each refusal: the field then says
    MIN_LEN_ERROR."""

    def __init__(self, parent=None, on_refused=None):
        super().__init__(parent)
        self.on_refused = on_refused

    def validate(self, text, pos):
        if is_min_len_text(text):
            return QValidator.State.Acceptable, text, pos
        if self.on_refused is not None:
            self.on_refused()
        return QValidator.State.Invalid, text, pos


def review_chip_text(sess) -> str:
    """The map chip on Keep: 'Area · 2.0 km² · 33 tiles'.  The area is the drawn one, as Draw, the run check and
    the map label gave it (the image's own area for "Use whole image", the drawn one for a rectangle), not the ground
    its tiles cover; a run reference without a drawn area gives the tiles' area."""
    km = sess.ref.drawn_km2 or sess.ref.area_km2
    return f"Area · {km2_label(km)} · {sess.tiles_words()}" if km else sess.tiles_words()


NO_PLACE_TEXT = "Couldn't place this run's lines on the map. The details are in the Road Extractor message log."


def teardown(ctx) -> None:
    """End the review for good: close the session (layers, highlight bands, map tool) and delete the map overlays
    with every hook they made.  Safe to call twice."""
    s = getattr(ctx, "review", None)
    ctx.review = None
    _unguard(ctx)
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
    ov = getattr(ctx, "_review_overlays", None)
    ctx._review_overlays = None
    if ov is not None:
        try:
            ov.delete()
        except Exception:
            pass
    try:
        ctx.iface.mapCanvas().refresh()
    except Exception:
        pass


def start_review(ctx, result: Dict[str, Any], summary: Optional[Dict[str, Any]] = None) -> None:
    """Open RV-Keep on a run result (called by the extract screens when a run ends)."""
    teardown(ctx)
    try:
        sess = ReviewSession(ctx, result, summary)
    except ValueError as e:
        _log(f"Review: the run result could not be shown: {type(e).__name__}: {e}")
        bar = getattr(ctx.iface, "messageBar", None)
        if callable(bar):
            bar().pushMessage("Road Extractor", NO_PLACE_TEXT, Qgis.MessageLevel.Warning, 8)
        return
    if not sess.has_lines():
        try:
            sess.close()
        except Exception:
            pass
        used = (summary or {}).get("tiles_used")
        run = getattr(ctx, "run", None)
        ctx.extras["nothing_tiles"] = int(used) if isinstance(used, (int, float)) and not isinstance(used, bool) \
            else int(getattr(run, "tiles_done", 0) or 0)
        ctx.go("EX-Failed", kind="nothing")
        return
    ctx.review = sess
    _guard(ctx)
    sess.lost.connect(lambda: review_lost(ctx, sess))
    ctx.go("RV-Keep")


def review_lost(ctx, sess) -> None:
    """The review layer or its group was removed by hand: the lines are gone.  End the review with a plain message
    and go to the start screen."""
    if getattr(ctx, "review", None) is not sess:
        return
    try:
        teardown(ctx)
    except Exception:
        ctx.review = None
    try:
        ctx.iface.messageBar().pushMessage(
            "Road Extractor", "The review layer was removed. The lines are gone.",
            Qgis.MessageLevel.Warning, 10)
    except Exception:
        pass
    if ctx.current_board.startswith("RV-") or ctx.dialogs.get("RV-Exit") is not None:
        ctx.close_dialog("RV-Exit")
        ctx.go("EX-Start")


_KEEP_BACK = ("EX-Start", "EX-NoImagery", "EX-DrawArea", "EX-Setup", "EX-OverBudget", "EX-Running", "EX-Failed")


def _guard(ctx) -> None:
    _unguard(ctx)
    ref = weakref.ref(ctx)

    def on_nav(board: str) -> None:
        c = ref()
        if c is None or board.startswith(("RV-", "ST-")):
            return
        QTimer.singleShot(0, lambda: _enforce(ref(), board))
    ctx._review_guard = on_nav
    ctx.navigated.connect(on_nav)
    lg = getattr(ctx, "leave_guard", None)
    if lg is not None:
        lg.hold("review")


def _unguard(ctx) -> None:
    lg = getattr(ctx, "leave_guard", None)
    if lg is not None:
        lg.release("review")
    g = getattr(ctx, "_review_guard", None)
    ctx._review_guard = None
    if g is not None:
        try:
            ctx.navigated.disconnect(g)
        except (RuntimeError, TypeError):
            pass


def _enforce(ctx, board: str) -> None:
    """The panel moved to another screen (its key given) while the review is still open: go back to RV-Keep or
    end the review."""
    if ctx is None:
        return
    s = getattr(ctx, "review", None)
    if s is None or s.closed or ctx.current_board.startswith(("RV-", "ST-")):
        return
    if board in _KEEP_BACK and s.has_lines():
        ctx.go("RV-Keep")
    else:
        teardown(ctx)


def _session(ctx) -> Optional[ReviewSession]:
    return getattr(ctx, "review", None)






def kbd(text: str, *, dark: bool = False, height: int = 20) -> QLabel:
    """The design's <kbd> chip: min 18 x 20 px, 4 px radius, mono 10 px (``dark``: on a lavender button)."""
    c = QLabel(text)
    c.setProperty("kbd", True)
    c.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    border = "rgba(22,13,36,0.35)" if dark else "rgba(247,245,251,0.18)"
    bg = "rgba(22,13,36,0.08)" if dark else "rgba(247,245,251,0.04)"
    col = "#160d24" if dark else theme.TEXT_2
    c.setStyleSheet(f"font-family:{MONO};font-size:10px;color:{col};border:1px solid {border};background:{bg};"
                    f"border-radius:4px;padding:0 5px;{'font-weight:500;' if dark else ''}")
    track(c, 0.4)
    c.setFixedHeight(height)
    c.setMinimumWidth(18)
    c.setAlignment(Qt.AlignmentFlag.AlignCenter)
    c.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return c


def _icon_label(name: str, size: int, colour: str, stroke: Optional[float] = None, width: Optional[int] = None) -> QLabel:
    lb = QLabel()
    lb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    lb.setPixmap(icons.pixmap(name, size, colour, stroke, width=width, recolor=True))
    lb.setFixedSize(width or size, size)
    return lb


def chip_button(text: str, kbds: List[str] = (), *, primary: bool = False, name: str = "",
                tooltip: str = "", danger: bool = False, height: int = 34, stacked: bool = False,
                icon: Optional[str] = None, icon_size: int = 15, icon_stroke: Optional[float] = None,
                trailing_icon: Optional[str] = None, plain_row: bool = False) -> QPushButton:
    """A button from the design with its icon and <kbd> chips (sizes from the design's CSS).

    * ``stacked`` (58 px tiles): icon top left, chip top right, label bottom left, radius 10, padding 8 / 10.
    * primary (lavender, 44 px pill): icon, label, chip on the right.
    * otherwise a 34 px outlined pill: icon, label (500), chips, optional trailing chevron.
    * ``plain_row`` (38 px rows inside a card): transparent, label 13 / 400."""
    from ..widgets import PillButton

    b = QPushButton(text) if (stacked or danger or plain_row) else PillButton(text)
    b.setObjectName(name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setMinimumHeight(height)
    b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    if tooltip:
        set_tip(b, tooltip)
    fg = "#160d24" if primary else (theme.DANGER if danger else theme.TEXT)
    ic_col = fg if (primary or danger) else theme.TEXT
    if stacked:
        lay = hbox(b, margins=(10, 8, 10, 0), spacing=4)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        if icon:
            lay.addWidget(_icon_label(icon, icon_size, ic_col, icon_stroke or 1.6), 0, Qt.AlignmentFlag.AlignTop)
        lay.addStretch(1)
        for k in kbds:
            lay.addWidget(kbd(k, dark=primary), 0, Qt.AlignmentFlag.AlignTop)
        border = "1px solid rgba(239,68,68,0.4)" if danger else f"1px solid {theme.BORDER_STRONG}"
        bg = "rgba(239,68,68,0.06)" if danger else theme.CARD_BG
        b.setStyleSheet(f"QPushButton{{text-align:left;border-radius:10px;background:{bg};border:{border};color:{fg};"
                        f"min-height:{height - 2}px;padding:26px 10px 8px 10px;font-size:12px;font-weight:500;}}")
        return b
    left = 18 if primary else (12 if plain_row else 10)
    lay = hbox(b, margins=(left, 0, 9 if primary else 8, 0), spacing=8)
    pad_left = left
    if icon:
        lay.addWidget(_icon_label(icon, icon_size, ic_col, icon_stroke or (2.0 if primary else 1.8)))
        pad_left = left + icon_size + (10 if primary else 8)
    lay.addStretch(1)
    for k in kbds:
        lay.addWidget(kbd(k, dark=primary))
    if trailing_icon:
        lay.addWidget(_icon_label(trailing_icon, 14, theme.TEXT_2, 1.8))
    if primary:
        b.setStyleSheet(f"QPushButton{{background:{theme.ACCENT};color:#160d24;border:none;font-weight:500;font-size:14px;"
                        f"text-align:left;padding-left:{pad_left}px;min-height:{height}px;}}")
    elif plain_row:
        b.setStyleSheet(f"QPushButton{{text-align:left;padding-left:{pad_left}px;background:transparent;border:none;"
                        f"border-radius:0;min-height:{height}px;font-size:13px;font-weight:400;}}")
    else:
        b.setStyleSheet(f"QPushButton{{text-align:left;padding-left:{pad_left}px;background:{theme.CARD_BG};"
                        f"min-height:{height - 2}px;font-size:13px;font-weight:500;}}")
    return b


def primary_pill(text: str, name: str, tooltip: str = "", arrow: str = "arrow-up-right") -> QPushButton:
    """The design's primary pill: label left, a round dark chip with an icon on the right."""
    from ..widgets import ArrowButton

    b = ArrowButton(text, arrow=arrow)
    b.setObjectName(name)
    if tooltip:
        set_tip(b, tooltip)
    return b


def secondary_pill(text: str, name: str, icon: Optional[str] = None) -> QPushButton:
    """Outlined 48 px pill (Back / Exit): padding 0 20, 14 / 500."""
    b = button(text, name=name)
    b.setMinimumHeight(48)
    pad = "0 20px"
    if icon:
        lay = hbox(b, margins=(20, 0, 0, 0), spacing=0)
        lay.addWidget(_icon_label(icon, 14, theme.TEXT, 1.8))
        lay.addStretch(1)
        pad = "0 20px 0 42px"
    b.setStyleSheet(f"QPushButton{{min-height:46px;font-size:14px;font-weight:500;padding:{pad};"
                    f"background:{theme.CARD_BG};border:1px solid {theme.BORDER_STRONG};}}")
    return b


def _own_mouse(w):
    """A map overlay keeps the mouse events it does not use: otherwise the viewport it sits on would hand them to
    the map tool."""
    w.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
    return w


def _delete_now(w) -> None:
    """Destroy a widget we own at once.  ``deleteLater`` leaves it alive until the event loop runs, and a widget
    still alive when QgsApplication goes away aborts the process at interpreter exit (segfault, exit 139)."""
    from qgis.PyQt import sip

    try:
        w.hide()
        w.setParent(None)
        sip.delete(w)
    except (RuntimeError, TypeError):
        pass


class _NoDoubleClick(QObject):
    """Swallows the second press of a double-click.  The first click changes the screen and the next screen's button
    sits under the cursor, so without this the second click presses it too (Next then Export, Export then New area)."""

    def eventFilter(self, o, ev):
        return ev.type() == QEvent.Type.MouseButtonDblClick


class _FocusOut(QObject):
    """Leaving the minimum-length field by Tab or a click elsewhere, also when its text did not change (Qt 6 then
    sends no editingFinished)."""

    def __init__(self, parent, on_out):
        super().__init__(parent)
        self._on_out = on_out

    def eventFilter(self, o, ev):
        if ev.type() == QEvent.Type.FocusOut:
            self._on_out()
        return False


class _RearmFilter(QObject):
    """Coming back to the panel (mouse in, click, focus) or to the QGIS window puts the review tool back on the
    canvas, the way the draw screen does for its rectangle tool.  The filter sits on the screen and the panel only (the
    plugin's own widgets); the QGIS window coming back to the front is QGuiApplication's focusWindowChanged signal,
    not a filter on the main window."""

    def __init__(self, screen):
        super().__init__(screen)
        self._ref = weakref.ref(screen)

    def rearm(self) -> None:
        scr = self._ref()
        if scr is None:
            return
        try:
            s = scr.sess
            if s is not None and not s.closed and scr.ctx.current_board == "RV-Keep" and s.rearm():
                scr._sync_tool_hint()
        except RuntimeError:
            pass

    def window_focused(self, win) -> None:
        scr = self._ref()
        if scr is None or win is None:
            return
        try:
            main = scr.ctx.iface.mainWindow()
            if main is not None and win is main.windowHandle():
                self.rearm()
        except (RuntimeError, AttributeError):
            pass

    def eventFilter(self, o, ev):
        if ev.type() in (QEvent.Type.Enter, QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn):
            self.rearm()
        return False


REARM_MARK = "_trb_rearm"


def _remove_filters(refs, flt) -> None:
    for r in refs:
        w = r()
        if w is not None:
            try:
                w.removeEventFilter(flt)
                w.setProperty(REARM_MARK, None)
            except RuntimeError:
                pass
    slot = getattr(flt, "window_focused", None)
    app = QGuiApplication.instance()
    if slot is not None and app is not None:
        try:
            app.focusWindowChanged.disconnect(slot)
        except (RuntimeError, TypeError):
            pass


class _Legend(QFrame):
    """Map legend: swatches from the design's SVG icons, mono 10 px caps labels, 18 px rows."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.setObjectName("mapLegend")
        self.setStyleSheet(f"QFrame#mapLegend{{background:rgba(7,8,14,0.82);border:1px solid {theme.HAIRLINE};"
                           f"border-radius:8px;}}")
        self._lay = vbox(self, margins=(12, 8, 12, 8), spacing=4)
        self.hide()

    def set_rows(self, rows) -> None:
        """rows: (icon file name, text) with the swatch width optional as a third item."""
        while self._lay.count():
            it = self._lay.takeAt(0)
            if it.widget() is not None:
                _delete_now(it.widget())
        for row in rows:
            name, text = row[0], row[1]
            w = row[2] if len(row) > 2 else None
            r = QWidget()
            hl = hbox(r, spacing=8)
            sw = QLabel()
            sw.setPixmap(icons.pixmap(name, 14 if w is None else 8, "#aaa4b5", None, width=w))
            sw.setFixedSize(w or 14, 14 if w is None else 8)
            tx = label(text, mono=True, wrap=False)
            tx.setStyleSheet(f"color:{theme.TEXT_2};font-family:{MONO};font-size:10px;")
            track(tx, 0.8, caps=True)
            hl.addWidget(sw)
            hl.addWidget(tx)
            r.setFixedHeight(18)
            self._lay.addWidget(r)
        self._lay.invalidate()
        self._lay.activate()
        self.resize(self.sizeHint())


UNDO_KEY = "⌘Z" if sys.platform == "darwin" else "Ctrl+Z"
DELETE_KEY = "⌫" if sys.platform == "darwin" else "Del"


def _fmt_len(m: float) -> str:
    return f"{m:.0f} m" if m < 1000 else f"{m / 1000.0:.2f} km"


class MapOverlays:
    """Pill (bottom centre), chip and legend (bottom left) drawn over the QGIS canvas, plus the review's own
    "Delete line" chip, line tag and "1 line deleted · Undo" toast.

    The overlays are children of the canvas's viewport, not of the canvas: Qt exposes a scroll area (the canvas is a
    QGraphicsView) to assistive tools through its viewport, scroll bars and corner widget only, so a child of the canvas
    itself would be missing from the accessibility tree and a screen reader could not find "Delete line" or "Undo
    delete".  They are made when the review opens its first screen (``overlays()``) and destroyed when it ends and at
    unload (``delete``).  Positions are worked out in the canvas's coordinates and moved onto the viewport by its
    offset, and each overlay keeps mouse events to itself (``WA_NoMousePropagation``), so a click, a wheel turn or a
    right click on an overlay never reaches the map tool under it.

    Every hook it makes (canvas signal, message-bar signals, session signals, timers) is undone by ``delete``, and every
    slot checks it is still alive, so a hook left over after the review ended never calls into a deleted widget."""

    def __init__(self, canvas, iface=None):
        self.canvas = canvas
        vp = canvas.viewport() if callable(getattr(canvas, "viewport", None)) else None
        self.host = vp if vp is not None else canvas
        self.iface = iface
        self._dead = False
        self._sess: Optional[ReviewSession] = None
        mk = lambda n: self._mk(n)
        self.pill = mk("mapPill")
        self.chip = mk("mapChip")
        self.legend = _own_mouse(_Legend(self.host))
        self.tag = mk("mapTag")
        self.tag.setStyleSheet(f"background:rgba(7,8,14,0.82);color:{theme.WARNING};border:1px solid {theme.WARNING};"
                               f"border-radius:2px;font-family:{MONO};font-size:10px;padding:3px 8px;")
        self._tag_pt = None
        self.callout: Optional[QWidget] = None
        self.line_tag = mk("mapLineTag")
        self.line_tag.setStyleSheet(f"background:rgba(7,8,14,0.82);color:{theme.ACCENT};border-radius:2px;"
                                    f"border:1px solid rgba(200,173,255,0.45);font-family:{MONO};font-size:10px;"
                                    f"padding:4px 10px;")
        self.delete_chip = self._mk_delete_chip()
        self.toast = self._mk_toast()
        try:
            QWidget.setTabOrder(canvas, self.delete_chip)
            QWidget.setTabOrder(self.delete_chip, self.toast_undo)
        except (RuntimeError, TypeError):
            pass
        self._toast_timer = QTimer()
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(7000)
        self._toast_timer.timeout.connect(self.hide_toast)
        self._hooks = []
        self._hook(canvas.extentsChanged, self.place)
        self._bar = MessageBarHook(self._on_message_bar, iface)

    def _hook(self, signal, slot) -> None:
        try:
            signal.connect(slot)
            self._hooks.append((signal, slot))
        except Exception:
            pass

    def _on_message_bar(self) -> None:
        if not self._dead:
            self.place()

    def _mk(self, name: str) -> QLabel:
        lb = _own_mouse(QLabel(self.host))
        lb.setObjectName(name)
        lb.setTextFormat(Qt.TextFormat.RichText)
        lb.setStyleSheet(f"background:rgba(7,8,14,0.82);color:{theme.TEXT};border:1px solid {theme.BORDER_STRONG};"
                         f"font-family:{MONO};font-size:10px;letter-spacing:1px;padding:4px 10px;")
        lb.hide()
        return caps(lb)

    def _mk_delete_chip(self) -> QPushButton:
        """The red pill: trash icon, "Delete line", the key cap."""
        b = _own_mouse(QPushButton("Delete line", self.host))
        b.setObjectName("deleteLineChip")
        set_tip(b, "Delete the whole line. Undo brings it back.")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        b.setFixedHeight(34)
        b.setStyleSheet("QPushButton{background:rgba(7,8,14,0.9);color:#f87171;border:1px solid rgba(239,68,68,0.4);"
                        "border-radius:17px;font-size:13px;font-weight:500;text-align:left;"
                        "padding:0 8px 0 35px;}")
        lay = hbox(b, margins=(12, 0, 8, 0), spacing=8)
        lay.addWidget(_icon_label("trash", 15, "#f87171", 1.6))
        lay.addStretch(1)
        key = kbd(DELETE_KEY)
        lay.addWidget(key)
        from qgis.PyQt.QtGui import QFont, QFontMetrics

        f = QFont(b.font())
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.Medium)
        b.setFont(f)
        text_w = QFontMetrics(f).horizontalAdvance("Delete line")
        b.setFixedWidth(35 + text_w + 14 + max(key.sizeHint().width(), 38) + 8)
        b.clicked.connect(self._on_delete_clicked)
        b.hide()
        return b

    def _mk_toast(self) -> QFrame:
        t = _own_mouse(QFrame(self.host))
        t.setObjectName("deleteToast")
        t.setStyleSheet(f"QFrame#deleteToast{{background:rgba(7,8,14,0.82);border:1px solid rgba(247,245,251,0.18);"
                        f"border-radius:16px;}}")
        lay = hbox(t, margins=(14, 6, 8, 6), spacing=12)
        self.toast_text = QLabel("1 line deleted")
        self.toast_text.setObjectName("toastText")
        self.toast_text.setStyleSheet(f"font-family:{MONO};font-size:11px;color:{theme.TEXT};background:transparent;"
                                      f"border:none;")
        track(self.toast_text, 0.9, caps=True)
        self.toast_undo = QPushButton("Undo")
        self.toast_undo.setObjectName("toastUndo")
        self.toast_undo.setAccessibleName("Undo delete")
        self.toast_undo.setAccessibleDescription("Bring back the line just deleted")
        self.toast_undo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toast_undo.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.toast_undo.setStyleSheet(f"QPushButton{{background:transparent;border:none;padding:0;color:{theme.ACCENT};"
                                      f"font-family:{MONO};font-size:11px;text-decoration:underline;}}")
        track(self.toast_undo, 0.9, caps=True)
        self.toast_undo.clicked.connect(self._on_undo_clicked)
        lay.addWidget(self.toast_text)
        lay.addWidget(self.toast_undo)
        lay.addWidget(kbd(UNDO_KEY))
        t.hide()
        return t

    def bind(self, sess: ReviewSession) -> None:
        self.unbind()
        self._sess = sess
        for sig, slot in ((sess.selection_changed, self._on_selection), (sess.line_deleted, self._on_deleted),
                          (sess.line_restored, self._on_restored), (sess.closing, self._on_closing),
                          (sess.changed, self._on_session_changed)):
            try:
                sig.connect(slot)
            except RuntimeError:
                pass

    def unbind(self) -> None:
        s, self._sess = self._sess, None
        if s is None:
            return
        for sig, slot in ((s.selection_changed, self._on_selection), (s.line_deleted, self._on_deleted),
                          (s.line_restored, self._on_restored), (s.closing, self._on_closing),
                          (s.changed, self._on_session_changed)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._hide_selection()
        self.hide_toast()

    def _on_closing(self) -> None:
        self.unbind()

    def _on_session_changed(self) -> None:
        self.place()

    def _on_selection(self, lid: int) -> None:
        if self._dead:
            return
        s = self._sess
        ln = s.lines.get(lid) if (s is not None and lid >= 0) else None
        if ln is None:
            self._hide_selection()
            return
        self.line_tag.setText(f"{theme.CLASS_NAMES.get(ln['class_id'], 'Line')} · {_fmt_len(ln['length_m'])}")
        self.line_tag.show()
        self.delete_chip.show()
        self.line_tag.raise_()
        self.delete_chip.raise_()
        self.place()

    def _hide_selection(self) -> None:
        self._focus_back_to_map(self.delete_chip)
        for w in (self.line_tag, self.delete_chip):
            try:
                w.hide()
            except RuntimeError:
                pass

    def _focus_back_to_map(self, w) -> None:
        """A keyboard user on a map control (it has the focus: only Tab gives it) goes back to the map when the
        control goes away, where Delete and Ctrl+Z work, not wherever Qt's focus chain leads next."""
        try:
            fw = w.window().focusWidget()
            if fw is not None and (fw is w or w.isAncestorOf(fw)):
                self.canvas.setFocus()
        except RuntimeError:
            pass

    def _on_deleted(self, _lid: int) -> None:
        if self._dead:
            return
        self.toast_text.setText("1 line deleted")
        self.toast.adjustSize()
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start()
        self.place()

    def _on_restored(self) -> None:
        self.hide_toast()

    def hide_toast(self) -> None:
        if self._dead:
            return
        self._toast_timer.stop()
        self._focus_back_to_map(self.toast)
        try:
            self.toast.hide()
        except RuntimeError:
            pass

    def _on_delete_clicked(self) -> None:
        if self._sess is not None:
            self._sess.handle_action("delete")

    def _on_undo_clicked(self) -> None:
        if self._sess is not None:
            self._sess.handle_action("undo")

    def set(self, pill: str = "", chip: str = "", legend: List[tuple] = ()) -> None:
        self.tag.hide()
        self._tag_pt = None
        for lb, text in ((self.pill, pill), (self.chip, chip)):
            lb.setVisible(bool(text))
            lb.setText(text)
        if legend:
            self.legend.set_rows(legend)
            self.legend.show()
        else:
            self.legend.hide()
        self.pill.setStyleSheet(self.pill.styleSheet().replace("border-radius:0px;", "") +
                                "border-radius:12px;font-size:11px;")
        self.place()
        QTimer.singleShot(0, self._place_later)

    def _place_later(self) -> None:
        if self._dead:
            return
        try:
            self.legend.resize(self.legend.sizeHint())
            self.place()
        except RuntimeError:
            pass

    def set_tag(self, text: str, map_pt=None) -> None:
        self._tag_pt = map_pt
        self.tag.setVisible(bool(text) and map_pt is not None)
        self.tag.setText(text.upper() if text else "")
        self.place()

    def set_callout(self, w: Optional[QWidget]) -> None:
        if self.callout is not None:
            _delete_now(self.callout)
        self.callout = w
        if w is not None:
            w.setParent(self.host)
            _own_mouse(w)
            w.show()
        self.place()

    def place(self) -> None:
        if self._dead:
            return
        try:
            self._place()
        except RuntimeError:
            pass

    def _move(self, w, x, y) -> None:
        """Put ``w`` at (x, y), given in the canvas's coordinates, on the canvas's viewport (the overlays'
        parent)."""
        if self.host is not self.canvas:
            o = self.host.pos()
            x, y = x - o.x(), y - o.y()
        w.move(int(x), int(y))

    def _place(self) -> None:
        cw, ch = self.canvas.width(), self.canvas.height()
        mv = self._move
        for w in (self.pill, self.chip):
            w.adjustSize()
        self.legend.resize(self.legend.sizeHint())
        top = 16 + message_bar_inset(self.canvas)
        mv(self.pill, max(0, (cw - self.pill.width()) // 2), top)
        mv(self.chip, 16, top)
        mv(self.legend, 16, max(0, ch - self.legend.height() - 16))
        if self._tag_pt is not None and self.tag.isVisibleTo(self.canvas):
            self.tag.adjustSize()
            pt = self.canvas.getCoordinateTransform().transform(self._tag_pt)
            mv(self.tag, int(pt.x()), max(0, int(pt.y()) - self.tag.height()))
        if self.callout is not None:
            mv(self.callout, max(0, cw - self.callout.width() - 16), max(0, ch - self.callout.height() - 16))
        s = self._sess
        sel = s.selected_id() if s is not None else None
        if sel is not None and self.delete_chip.isVisibleTo(self.canvas):
            a = s.line_anchor_canvas_px(sel)
            if a is not None:
                self.line_tag.adjustSize()
                bw, bh = self.delete_chip.width(), self.delete_chip.height()
                x = min(max(8, a[0]), max(8, cw - bw - 8))
                y = a[1] - 12 - bh
                ty = y - 4 - self.line_tag.height()
                if ty < 8:
                    y = a[1] + 14
                    ty = y + bh + 4
                mv(self.delete_chip, x, y)
                mv(self.line_tag, x, ty)
        if self.toast.isVisibleTo(self.canvas):
            self.toast.adjustSize()
            mv(self.toast, max(0, (cw - self.toast.width()) // 2), max(0, ch - self.toast.height() - 16))

    def clear(self) -> None:
        for w in (self.pill, self.chip, self.legend, self.tag):
            w.hide()
        self._tag_pt = None
        self.set_callout(None)
        self.unbind()

    def delete(self) -> None:
        """Take every widget and every hook back out (idempotent)."""
        if self._dead:
            return
        self.clear()
        self._dead = True
        self._toast_timer.stop()
        self._bar.dispose()
        for sig, slot in self._hooks:
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._hooks = []
        for w in (self.pill, self.chip, self.legend, self.tag, self.line_tag, self.delete_chip, self.toast):
            _delete_now(w)


class _StepBadge(QWidget):
    """The 20 px step circle: solid lavender with a dark number (current), outlined green tick (done), outlined
    grey number (to do), as in the design's CSS."""

    def __init__(self, n: int, state: str):
        super().__init__()
        self.n, self.state = n, state
        self.setFixedSize(20, 20)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _e):
        from qgis.PyQt.QtGui import QFont

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, 19, 19)
        if self.state == "current":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.ACCENT))
            p.drawEllipse(QRectF(0, 0, 20, 20))
            col = QColor("#160d24")
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(112, 223, 173, 115) if self.state == "done" else QColor(247, 245, 251, 77), 1))
            p.drawEllipse(r)
            col = QColor(theme.TEXT_2)
        if self.state == "done":
            p.drawPixmap(4, 4, icons.pixmap("check-tight", 11, theme.SUCCESS, 2.6))
            return
        f = QFont("IBM Plex Mono")
        f.setPixelSize(11)
        f.setWeight(QFont.Weight.Medium if self.state == "current" else QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(col)
        p.drawText(QRectF(0, 0, 20, 20), int(Qt.AlignmentFlag.AlignCenter), str(self.n))


class StepNav(QWidget):
    """Keep / Export ladder: 40 px pills, radius 10, 20 px badge + 13 / 500 label."""

    def __init__(self, ctx, current: str):
        super().__init__()
        self.setObjectName("stepNav")
        lay = hbox(self, spacing=6)
        order = ["keep", "export"]
        names = {"keep": ("Keep", "RV-Keep", "stepKeep"), "export": ("Export", "RV-Export", "stepExport")}
        idx = order.index(current)
        for i, k in enumerate(order):
            text, board, name = names[k]
            state = "done" if i < idx else ("current" if i == idx else "todo")
            b = QPushButton(text)
            b.setObjectName(name)
            b.setAccessibleName(f"{text} step")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(40)
            b.setProperty("stepState", state)
            if state == "done":
                set_tip(b, "Done. Open this step again.")
            else:
                b.setAccessibleDescription("Current step" if state == "current" else "")
            hb = hbox(b, margins=(10, 0, 0, 0), spacing=0)
            hb.addWidget(_StepBadge(i + 1, state), 0, Qt.AlignmentFlag.AlignVCenter)
            hb.addStretch(1)
            if state == "current":
                look = f"border:1px solid rgba(200,173,255,0.45);background:rgba(139,92,246,0.12);color:{theme.TEXT};"
            else:
                look = f"border:1px solid {theme.HAIRLINE};background:rgba(247,245,251,0.03);color:{theme.TEXT_2};"
            b.setStyleSheet(f"QPushButton{{{look}border-radius:10px;min-height:38px;font-size:13px;font-weight:500;"
                            f"padding:0 8px 0 38px;text-align:left;}}")
            b.clicked.connect(lambda _=False, bd=board: ctx.go(bd))
            lay.addWidget(b, 1)


_CONTROLS_QSS = f"""
QCheckBox, QLineEdit, QComboBox, QPushButton, QLabel {{ font-size: 13px; }}
QLabel[mono="true"] {{ font-size: 10px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({icons.png_url("chevron-down", 14, theme.TEXT, 1.8)}); width: 14px; height: 14px; }}
QCheckBox {{ spacing: 0; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid rgba(247,245,251,0.5); border-radius: 3px;
    background: #f7f5fb; }}
QCheckBox::indicator:checked {{ background: {theme.ACCENT}; border-color: {theme.ACCENT}; image: url({icons.checkmark_url()}); }}
"""


class ReviewScreen(Screen):
    """Scrollable body + pinned footer.  Subclasses fill ``self.body`` and ``self.footer``."""

    step = "keep"
    overlays_wanted = True

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self._overlay: Optional[MapOverlays] = None
        root = vbox(self, spacing=0)
        sc = FitScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("reviewBody")
        sc.setStyleSheet(f"QScrollArea{{background:{theme.BG_COVER};border:none;}}"
                         f"QWidget#reviewBody{{background:{theme.BG_COVER};}}" + _CONTROLS_QSS + SCROLLBAR_QSS)
        self.body = vbox(inner, margins=(14, 14, 14, 8), spacing=10)
        sc.setWidget(inner)
        sc.viewport().setObjectName("reviewViewport")
        sc.viewport().setStyleSheet(f"QWidget#reviewViewport{{background:{theme.BG_COVER};}}")
        root.addWidget(sc, 1)
        foot = QWidget()
        foot.setObjectName("reviewFooter")
        foot.setStyleSheet(f"QWidget#reviewFooter{{background:{theme.BG_COVER};}}")
        self.footer = hbox(foot, margins=(14, 4, 14, 16), spacing=8)
        root.addWidget(foot)
        self.steps_holder = vbox(spacing=0)
        self.nav: Optional[StepNav] = None

    def add_nav(self) -> None:
        if self.nav is not None:
            self.nav.deleteLater()
        self.nav = StepNav(self.ctx, self.step)
        self.body.insertWidget(0, self.nav)

    @property
    def sess(self) -> Optional[ReviewSession]:
        return _session(self.ctx)

    def overlays(self) -> MapOverlays:
        cv = self.ctx.iface.mapCanvas()
        ov = getattr(self.ctx, "_review_overlays", None)
        if ov is None or ov.canvas is not cv:
            if ov is not None:
                ov.delete()
            ov = MapOverlays(cv, self.ctx.iface)
            self.ctx._review_overlays = ov
        return ov

    def before_close(self) -> bool:
        """Close x on any review step: ask RV-Exit when lines exist, else drop the review and close."""
        s = self.sess
        if s is None or not s.has_lines():
            self.drop_review()
            return True
        self.ctx.go("RV-Exit")
        return False

    def drop_review(self) -> None:
        teardown(self.ctx)

    def on_hide(self) -> None:
        pass

    def exit_to_dialog(self) -> None:
        self.ctx.go("RV-Exit")


def caps(w: QLabel) -> QLabel:
    """Render in capitals like the design's CSS text-transform, keeping the real copy in the widget."""
    from qgis.PyQt.QtGui import QFont
    f = w.font()
    f.setCapitalization(QFont.Capitalization.AllUppercase)
    w.setFont(f)
    return w


def _card_title(text: str, name: str = "") -> SectionHeading:
    h = SectionHeading(text)
    h.text.setText(text)
    caps(h.text)
    if name:
        h.setObjectName(name)
    return h




def _passthrough(w: QWidget) -> None:
    """Let clicks on ``w`` and everything inside it reach the button it is drawn on."""
    w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    for c in w.findChildren(QWidget):
        c.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


class LengthByClass(QFrame):
    """The "Length by class · km" card: one row per line type that has lines, a bar and the km.  Used on RV-Export
    and RV-Recap."""

    def __init__(self, name: str = "lengthCard"):
        super().__init__()
        self.setProperty("card", True)
        self.setObjectName(name)
        self._lay = vbox(self, margins=(14, 14, 14, 14), spacing=6)
        self._lay.addWidget(_card_title("Length by class · km"))
        self._rows = QWidget()
        self._rows_lay = vbox(self._rows, spacing=6)
        self._lay.addWidget(self._rows)

    def set_values(self, by_km: Dict[int, float]) -> None:
        while self._rows_lay.count():
            it = self._rows_lay.takeAt(0)
            if it.widget() is not None:
                _delete_now(it.widget())
        shown = [(cid, colour, name) for cid, _k, name, colour in theme.CLASSES if by_km.get(cid, 0.0) > 0]
        mx = max([by_km[c] for c, _c, _n in shown] + [0.0]) or 1.0
        for cid, colour, name in shown:
            v = float(by_km[cid])
            r = QWidget()
            r.setObjectName(f"lenRow_{cid}")
            r.setFixedHeight(24)
            rl = hbox(r, spacing=0)
            rl.addWidget(class_swatch(cid, 10, 3))
            rl.addSpacing(8 - Swatch.PAD)
            nm = label(name, wrap=False)
            nm.setStyleSheet("font-size:12px;")
            nm.setFixedWidth(84)
            rl.addWidget(nm)
            rl.addSpacing(8)
            bar = Meter(4)
            bar.setObjectName(f"lenBar_{cid}")
            bar.set_segments([(v / mx, colour)])
            rl.addWidget(bar, 1)
            rl.addSpacing(8)
            km = label(_fmt_km(v), wrap=False, name=f"lenKm_{cid}")
            km.setFixedWidth(52)
            km.setAlignment(Qt.AlignmentFlag.AlignRight)
            km.setStyleSheet(f"font-family:{MONO};font-size:12px;")
            rl.addWidget(km)
            self._rows_lay.addWidget(r)


@registry.screen("RV-Keep")
class RvKeep(ReviewScreen):
    step = "keep"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.add_nav()
        row = hbox()
        self.summary = label("", name="summaryLines", wrap=False)
        self.summary.setStyleSheet("font-size:15px;font-weight:500;")
        self.summary_tiles = caps(label("", mono=True, name="summaryTiles", wrap=False))
        row.addWidget(self.summary, 1)
        row.addWidget(self.summary_tiles)
        self.body.addLayout(row)
        self.summary_note = QLabel()
        self.summary_note.setObjectName("summaryNote")
        self.summary_note.setWordWrap(True)
        self.summary_note.setStyleSheet(f"font-size:12px;color:{theme.WARNING};")
        self.summary_note.hide()
        self.body.addWidget(self.summary_note)
        c2 = card(name="classesCard")
        l2 = vbox(c2, margins=(14, 14, 14, 14), spacing=0)
        h2 = hbox()
        h2.addWidget(_card_title("Lines to keep"), 1)
        h2.addWidget(label("KM", mono=True, wrap=False, name="kmTag"))
        l2.addLayout(h2)
        l2.addSpacing(8)
        self.class_checks: Dict[int, QCheckBox] = {}
        self.class_km: Dict[int, QLabel] = {}
        self.class_rows: Dict[int, QWidget] = {}
        self.class_fx: Dict[int, QGraphicsOpacityEffect] = {}
        for cid, _k, name, _colour in theme.CLASSES:
            r = QWidget()
            r.setObjectName(f"classRow_{cid}")
            rl = hbox(r, spacing=9 - Swatch.PAD)
            cb = QCheckBox()
            cb.setObjectName(f"classCheck_{cid}")
            cb.setAccessibleName(f"Keep {name} lines")
            cb.setChecked(cid not in DEFAULT_OFF)
            cb.toggled.connect(lambda on, c_=cid: self._class_toggled(c_, on))
            sw = class_swatch(cid, 18, 3)
            nm = label(name, wrap=False, name=f"className_{cid}")
            nm.setStyleSheet("font-size:14px;font-weight:400;")
            km = label("0", wrap=False, name=f"classKm_{cid}")
            km.setStyleSheet(f"font-family:{MONO};font-size:12px;color:{theme.TEXT_2};")
            for w in (cb, sw, nm):
                rl.addWidget(w)
            rl.addStretch(1)
            rl.addWidget(km)
            r.setMinimumHeight(26)
            eff = QGraphicsOpacityEffect(sw)
            sw.setGraphicsEffect(eff)
            self.class_fx[cid] = eff
            l2.addWidget(r)
            self.class_checks[cid], self.class_km[cid], self.class_rows[cid] = cb, km, r
        self.body.addWidget(c2)
        c4 = card(name="removeCard")
        l4 = vbox(c4, margins=(14, 14, 14, 14), spacing=8)
        l4.addWidget(_card_title("Remove lines"))
        self.remove_hint = label("Click a line on the map, then press Delete to remove the whole line. "
                                 "Undo brings it back.", muted=True, name="removeHint")
        self.remove_hint.setStyleSheet(f"font-size:13px;color:{theme.TEXT_2};")
        l4.addWidget(self.remove_hint)
        self.delete_row = QWidget()
        self.delete_row.setObjectName("deleteSelectedRow")
        dsr = hbox(self.delete_row, spacing=10)
        self.delete_btn = QPushButton("Delete selected line")
        self.delete_btn.setObjectName("deleteSelectedButton")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_tip(self.delete_btn, "Delete the line selected on the map. Undo brings it back.")
        self.delete_btn.setStyleSheet(f"QPushButton{{border:none;background:transparent;padding:0;color:{theme.ACCENT};"
                                      f"font-size:13px;font-weight:500;text-decoration:underline;}}"
                                      f"QPushButton:disabled{{color:{theme.TEXT_MUTED};text-decoration:none;}}")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._delete_selected)
        dsr.addWidget(self.delete_btn)
        dsr.addWidget(kbd(DELETE_KEY))
        dsr.addStretch(1)
        l4.addWidget(self.delete_row)
        self.tool_off = QWidget()
        self.tool_off.setObjectName("toolOffRow")
        tr = hbox(self.tool_off, spacing=10)
        off_lb = label("Line selection is off.", muted=True, name="toolOffHint", wrap=False)
        off_lb.setStyleSheet(f"font-size:13px;color:{theme.WARNING};")
        self.select_btn = QPushButton("Select lines")
        self.select_btn.setObjectName("selectLinesButton")
        self.select_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_btn.setStyleSheet(f"QPushButton{{border:none;background:transparent;padding:0;color:{theme.ACCENT};"
                                      f"font-size:13px;font-weight:500;text-decoration:underline;}}")
        self.select_btn.clicked.connect(self._select_lines)
        tr.addWidget(off_lb)
        tr.addWidget(self.select_btn)
        tr.addStretch(1)
        l4.addWidget(self.tool_off)
        self.tool_off.hide()
        self.deleted_row = QWidget()
        self.deleted_row.setObjectName("deletedRow")
        dr = hbox(self.deleted_row, spacing=10)
        self.deleted_label = QLabel()
        self.deleted_label.setObjectName("deletedCount")
        self.deleted_label.setTextFormat(Qt.TextFormat.RichText)
        self.deleted_label.setStyleSheet("font-size:13px;font-weight:500;")
        self.undo_link = QPushButton("Undo")
        self.undo_link.setObjectName("undoLink")
        self.undo_link.setCursor(Qt.CursorShape.PointingHandCursor)
        set_tip(self.undo_link, "Bring back the last deleted line")
        self.undo_link.setStyleSheet(f"QPushButton{{border:none;background:transparent;padding:0;color:{theme.ACCENT};"
                                     f"font-size:13px;font-weight:500;text-decoration:underline;}}")
        self.undo_link.clicked.connect(self._undo)
        dr.addWidget(self.deleted_label, 1)
        dr.addWidget(self.undo_link)
        dr.addWidget(kbd(UNDO_KEY))
        l4.addWidget(self.deleted_row)
        self.deleted_row.hide()
        self.body.addWidget(c4)
        self.min_toggle = QPushButton()
        self.min_toggle.setObjectName("minLengthToggle")
        self.min_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.min_toggle.setFixedHeight(44)
        self.min_toggle.setAccessibleName("Minimum length")
        self.min_toggle.setToolTip(MIN_TIP)
        self.min_toggle.setStyleSheet(f"QPushButton{{background:{theme.CARD_BG};border:1px solid {theme.HAIRLINE};"
                                      f"border-radius:12px;}}")
        tl = hbox(self.min_toggle, margins=(14, 0, 12, 0), spacing=10)
        head = SectionHeading("Minimum length")
        head.text.setText("Minimum length")
        caps(head.text)
        self.min_value = label("0 m", wrap=False, name="minLengthValue")
        self.min_value.setStyleSheet(f"font-family:{MONO};font-size:12px;color:{theme.TEXT_2};")
        self.min_chevron = _icon_label("chevron-down", 14, theme.TEXT_2, 1.8)
        tl.addWidget(head, 1)
        tl.addWidget(self.min_value)
        tl.addWidget(self.min_chevron)
        _passthrough(head)
        _passthrough(self.min_value)
        self.min_toggle.clicked.connect(lambda: self.set_min_open(not self.min_body.isVisibleTo(self)))
        self.body.addWidget(self.min_toggle)
        self.min_body = card(name="minLengthCard")
        l3 = vbox(self.min_body, margins=(14, 12, 14, 12))
        r3 = hbox(spacing=8)
        r3.addWidget(label("Drop lines shorter than", wrap=False, name="minLengthLabel"))
        self.min_input = QLineEdit("0")
        self.min_input.setObjectName("minLengthInput")
        self.min_input.setAccessibleName("Drop lines shorter than, in metres")
        self.min_input.setAccessibleDescription("0 keeps every line.")
        self.min_input.setFixedWidth(52)
        self.min_input.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.min_input.setPlaceholderText("0")
        self.min_input.setValidator(MinLenValidator(self.min_input, on_refused=self._min_rejected))
        self.min_input.setMaxLength(8)
        self.min_input.textChanged.connect(self._min_changed)
        self.min_input.editingFinished.connect(self._min_done)
        self.min_input.installEventFilter(_FocusOut(self.min_input, self._min_done))
        r3.addWidget(self.min_input)
        r3.addWidget(label("m", wrap=False, name="minLengthUnit"))
        r3.addStretch(1)
        self.min_hidden = label("", name="minLengthHidden", wrap=False)
        self.min_hidden.setStyleSheet(f"font-family:{MONO};font-size:12px;color:{theme.TEXT_2};")
        r3.addWidget(self.min_hidden)
        l3.addLayout(r3)
        self.min_error = label("", name="minLengthError")
        self.min_error.setStyleSheet(f"font-size:12px;color:{theme.WARNING};")
        self.min_error.hide()
        l3.addWidget(self.min_error)
        self.body.addWidget(self.min_body)
        self.min_body.hide()
        self.body.addStretch(1)
        self.exit_btn = secondary_pill("Exit", "exitButton")
        self.exit_btn.clicked.connect(lambda: ctx.go("RV-Exit"))
        self.next_btn = primary_pill("Next", "nextButton")
        self.next_btn.installEventFilter(_NoDoubleClick(self.next_btn))
        self.next_btn.clicked.connect(lambda: ctx.go("RV-Export"))
        self.footer.addWidget(self.exit_btn)
        self.footer.addWidget(self.next_btn, 1)
        self._bound = None
        self._rearm = _RearmFilter(self)
        self._rearm_on: List[Any] = []
        for w in (self, getattr(ctx, "panel", None)):
            if w is not None:
                w.installEventFilter(self._rearm)
                self._rearm_on.append(weakref.ref(w))
        app = QGuiApplication.instance()
        if app is not None:
            app.focusWindowChanged.connect(self._rearm.window_focused)
        self.destroyed.connect(lambda *_a, refs=self._rearm_on, f=self._rearm: _remove_filters(refs, f))

    def _watch_panel_presses(self) -> None:
        """A press anywhere in the panel takes the review tool back.  A control keeps its own press (a button, a
        tick, the minimum-length field), so the filter on the screen and the panel never sees it: the same filter goes
        on every control of the panel too (the plugin's own widgets only; taken off when this screen goes)."""
        from .panel import _takes_input

        for root in (self, getattr(self.ctx, "panel", None)):
            if root is None:
                continue
            try:
                kids = root.findChildren(QWidget)
            except RuntimeError:
                continue
            for w in kids:
                try:
                    if w.property(REARM_MARK) or not _takes_input(w):
                        continue
                    w.installEventFilter(self._rearm)
                    w.setProperty(REARM_MARK, True)
                    self._rearm_on.append(weakref.ref(w))
                except RuntimeError:
                    pass

    def _select_lines(self) -> None:
        s = self.sess
        if s is not None and not s.closed:
            s.rearm(force=True)
            self._sync_tool_hint()

    def _sync_tool_hint(self, active: Optional[bool] = None) -> None:
        s = self.sess
        if s is None or s.closed:
            self.tool_off.hide()
            return
        if active is None:
            active = s.tool_active()
        self.tool_off.setVisible(not active)

    def showEvent(self, e) -> None:
        """The panel was closed and reopened (or Settings closed) with Keep on screen: take the map tool back."""
        super().showEvent(e)
        s = self.sess
        if s is not None and not s.closed and self.ctx.current_board == "RV-Keep":
            s.rearm()
            self._sync_tool_hint()

    def set_min_open(self, on: bool) -> None:
        self.min_body.setVisible(on)
        self.min_toggle.setAccessibleDescription(f"{'Expanded' if on else 'Collapsed'}. {MIN_TIP}")

    def _unbind(self) -> None:
        old, self._bound = self._bound, None
        if old is not None:
            try:
                old.changed.disconnect(self.refresh)
            except (RuntimeError, TypeError):
                pass
            try:
                old.tool_state.disconnect(self._sync_tool_hint)
            except (RuntimeError, TypeError):
                pass
            try:
                old.selection_changed.disconnect(self._sync_delete)
            except (RuntimeError, TypeError):
                pass

    def _sync_delete(self, *_a) -> None:
        """Delete selected line is on while a line is selected (a selection change emits no ``changed``)."""
        s = self.sess
        self.delete_btn.setEnabled(s is not None and not s.closed and s.selected_id() is not None)

    def _delete_selected(self) -> None:
        s = self.sess
        if s is None or s.closed or s.selected_id() is None:
            return
        had_focus = self.delete_btn.hasFocus()
        s.handle_action("delete")
        if had_focus and self.undo_link.isVisibleTo(self) and self.undo_link.isEnabled():
            self.undo_link.setFocus()

    def on_show(self, **kw) -> None:
        s = self.sess
        if s is None:
            return
        s.step = "keep"
        if self._bound is not s:
            self._unbind()
            self._bound = s
            s.changed.connect(self.refresh)
            s.tool_state.connect(self._sync_tool_hint)
            s.selection_changed.connect(self._sync_delete)
        self.min_input.blockSignals(True)
        self.min_input.setText(min_len_text(s.min_len_m))
        self.min_input.blockSignals(False)
        self._min_say("")
        for cid, cb in self.class_checks.items():
            cb.blockSignals(True)
            cb.setChecked(s.class_on[cid])
            cb.blockSignals(False)
        self.set_min_open(False)
        s.zoom_to_area()
        ov = self.overlays()
        ov.set(chip=review_chip_text(s))
        ov.bind(s)
        s.activate_tool()
        self._watch_panel_presses()
        self._sync_tool_hint()
        self.refresh()

    def on_hide(self) -> None:
        s = self.sess
        if s is not None and not s.closed:
            s.deselect()
            s.release_tool()
        self.tool_off.hide()

    def refresh(self) -> None:
        s = self.sess
        if s is None or s.closed:
            return
        c = s.counts()
        self.summary.setText(f"{_plural(c['lines'], 'line', 'lines')} · {length_text(c['km'])}")
        self.summary_tiles.setText(_tiles_caption(s))
        note = held_note(s) if getattr(s, "partial", False) else (
            stopped_note(s.tiles_charged, s.tiles_planned) if s.stopped else "")
        self.summary_note.setText(note)
        self.summary_note.setVisible(bool(note))
        for cid in range(1, 8):
            km = c["km_by_class"].get(cid, 0.0)
            self.class_km[cid].setText(_fmt_km(km))
            on = s.class_on[cid]
            self.class_rows[cid].setStyleSheet("" if on else "QLabel{color:rgba(247,245,251,0.55);}")
            self.class_fx[cid].setOpacity(1.0 if on else 0.45)
        n = s.deleted_count
        self._sync_tool_hint()
        self._sync_delete()
        self.deleted_row.setVisible(n > 0)
        self.deleted_label.setText(f"<span style=\"font-family:'IBM Plex Mono'\">{count(n)}</span> deleted")
        self.undo_link.setEnabled(s.can_undo())
        m = s.min_len_m
        self.min_value.setText(f"{count(m) if m == int(m) else min_len_text(m)} m")
        self.min_hidden.setText(f"{count(c['len_hidden'])} hidden")

    def _undo(self) -> None:
        if self.sess:
            self.sess.undo()

    def _class_toggled(self, cid: int, on: bool) -> None:
        if self.sess:
            self.sess.set_class_on(cid, on)
            self.refresh()

    def _min_changed(self, text: str) -> None:
        """Every text the field takes is a length from 0 to MIN_LEN_MAX_M and is applied at once (an empty
        field is 0: every line kept).  A text set from code that is not one (the validator does not judge setText) is
        put back to the length in use, and the field says so: the field and the filter never differ."""
        s = self.sess
        if not is_min_len_text(text):
            self._min_say(MIN_LEN_ERROR)
            if s is not None:
                self._min_show(s.min_len_m)
            return
        if s is not None:
            s.set_min_len(parse_min_len(text))
            self.refresh()

    def _min_rejected(self) -> None:
        """A key, a paste or a set value the field did not take (a minus sign, a letter, a third decimal, more than
        MIN_LEN_MAX_M): said under the field until it is left."""
        self._min_say(MIN_LEN_ERROR)

    def _min_done(self) -> None:
        """Enter, Tab or a click elsewhere: the field shows the length in use in its plain form ('' and '12.' become
        '0' and '12', '1,5' becomes '1.5'; the value is the same) and the note under it goes."""
        self._min_say("")
        s = self.sess
        if s is not None and self.min_input.text() != min_len_text(s.min_len_m):
            self._min_show(s.min_len_m)

    def _min_show(self, m: float) -> None:
        self.min_input.blockSignals(True)
        self.min_input.setText(min_len_text(m))
        self.min_input.blockSignals(False)

    def _min_say(self, text: str) -> None:
        self.min_error.setText(text)
        self.min_error.setVisible(bool(text))


@registry.screen("RV-Export")
class RvExport(ReviewScreen):
    step = "export"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.add_nav()
        row = hbox()
        self.summary = label("", name="summaryLines", wrap=False)
        self.summary.setStyleSheet("font-size:15px;font-weight:500;")
        self.summary_tiles = caps(label("", mono=True, name="summaryTiles", wrap=False))
        row.addWidget(self.summary, 1)
        row.addWidget(self.summary_tiles)
        self.body.addLayout(row)
        e = card(name="exportCard")
        el = vbox(e, margins=(14, 14, 14, 14))
        el.addWidget(_card_title("Export"))
        g = hbox(spacing=10)
        col1 = vbox(spacing=5)
        col1.addWidget(caps(label("Layer name", mono=True, wrap=False)))
        self.name_in = QLineEdit()
        self.name_in.setObjectName("layerNameInput")
        self.name_in.setAccessibleName("Layer name")
        self.name_in.setFixedHeight(40)
        self.name_in.textEdited.connect(self._name_edited)
        col1.addWidget(self.name_in)
        col2 = vbox(spacing=5)
        col2.addWidget(caps(label("Format", mono=True, wrap=False)))
        self.fmt = QComboBox()
        self.fmt.setObjectName("formatCombo")
        name_combo(self.fmt, "Format")
        self.fmt.addItems(list(export_mod.FORMATS))
        self.fmt.setFixedSize(132, 40)
        style_combo_popup(self.fmt)
        self.fmt.currentTextChanged.connect(self._fmt_changed)
        col2.addWidget(self.fmt)
        g.addLayout(col1, 1)
        g.addLayout(col2)
        el.addLayout(g)
        self.name_note = label("", muted=True, name="nameNote")
        self.name_note.setStyleSheet(f"font-size:12px;color:{theme.TEXT_2};")
        self.name_note.hide()
        el.addWidget(self.name_note)
        self.crs_note = label("", muted=True, name="crsNote")
        self.crs_note.setStyleSheet(f"font-size:12px;color:{theme.TEXT_2};")
        self.crs_note.hide()
        el.addWidget(self.crs_note)
        el.addWidget(caps(label("Save to", mono=True, wrap=False)))
        sr = hbox(spacing=8)
        self.folder_in = QLineEdit()
        self.folder_in.setObjectName("saveToInput")
        self.folder_in.setAccessibleName("Save to")
        self.folder_in.setReadOnly(True)
        self.folder_in.setFixedHeight(40)
        self.folder_in.textChanged.connect(self._folder_shown)
        self.browse_btn = button("Browse…", name="browseButton")
        self.browse_btn.setAccessibleName("Browse for a folder")
        self.browse_btn.setMinimumHeight(40)
        self.browse_btn.setStyleSheet(f"QPushButton{{min-height:38px;padding:0 16px;font-size:13px;font-weight:500;"
                                      f"background:{theme.CARD_BG};border:1px solid {theme.BORDER_STRONG};}}")
        self.browse_btn.clicked.connect(self.browse)
        sr.addWidget(self.folder_in, 1)
        sr.addWidget(self.browse_btn)
        el.addLayout(sr)
        self.add_map = QCheckBox("Add to map")
        self.add_map.setStyleSheet("QCheckBox{font-size:14px;font-weight:400;spacing:8px;}")
        self.add_map.setObjectName("addToMapCheck")
        self.add_map.setChecked(True)
        self.add_map.toggled.connect(self._add_toggled)
        el.addWidget(self.add_map)
        self.export_err = label("", danger=True, name="exportError")
        self.export_err.hide()
        el.addWidget(self.export_err)
        self.body.addWidget(e)
        self.lengths = LengthByClass("lengthCard")
        self.body.addWidget(self.lengths)
        self.body.addStretch(1)
        self.back_btn = secondary_pill("Back", "backButton", icon="arrow-left")
        self.back_btn.clicked.connect(lambda: ctx.go("RV-Keep"))
        self.export_btn = primary_pill("Export", "exportButton", arrow="download")
        self.export_btn.installEventFilter(_NoDoubleClick(self.export_btn))
        self.export_btn.clicked.connect(self.do_export)
        self.footer.addWidget(self.back_btn)
        self.footer.addWidget(self.export_btn, 1)
        self._loading = False

    def _name_edited(self, text: str) -> None:
        s = self.sess
        if s is None or self._loading:
            return
        s.export_name, s.export_name_edited = text, True
        self._refresh_target()

    def _fmt_changed(self, *a) -> None:
        s = self.sess
        if s is None or self._loading:
            return
        s.export_format = self.fmt.currentText()
        self._refresh_target()

    def _folder_shown(self, text: str) -> None:
        """Whatever puts a folder into "Save to" (Browse…, or an accessibility tool setting the field's text): the export
        goes where the field says, when that is an existing folder."""
        s = self.sess
        if s is None or self._loading or text == getattr(self, "_shown_folder", None):
            return
        d = os.path.expanduser((text or "").strip())
        if d and d != s.export_folder and os.path.isdir(d):
            s.export_folder, s.export_folder_edited = d, True
            self._shown_folder = text
            self._refresh_note()

    def _add_toggled(self, on: bool) -> None:
        if self.sess is not None and not self._loading:
            self.sess.export_add_to_map = bool(on)

    def _refresh_target(self) -> None:
        """The full folder path, and a note when the typed name is taken (the export then adds a number)."""
        s = self.sess
        if s is None:
            return
        self._shown_folder = s.export_folder
        self.folder_in.setText(s.export_folder)
        self._refresh_note()

    def _refresh_note(self) -> None:
        """The notes under the card and the hover texts, from the folder, name and format the export will use."""
        s = self.sess
        if s is None:
            return
        set_tip(self.folder_in, s.export_folder)
        if s.export_format == "GeoJSON":
            self.crs_note.setText("GeoJSON is saved as longitude and latitude (EPSG:4326).")
            self.crs_note.show()
        else:
            self.crs_note.hide()
        typed = (s.export_name or "").strip() or export_mod.default_layer_name()
        try:
            final, path = s.resolve_target(typed, s.export_format)
        except Exception:
            self.name_note.hide()
            return
        if final != typed:
            self.name_note.setText(f"{typed} already exists. This one is saved as {final}.")
            self.name_note.show()
        else:
            self.name_note.hide()
        set_tip(self.export_btn, f"Writes {path}")

    def browse(self) -> None:
        s = self.sess
        if s is None:
            return
        d = QFileDialog.getExistingDirectory(self.ctx.iface.mainWindow(), "Save to", s.export_folder)
        if d:
            s.export_folder, s.export_folder_edited = d, True
            self.export_err.hide()
            self._refresh_target()

    def on_show(self, **kw) -> None:
        ov = getattr(self.ctx, "_review_overlays", None)
        if ov is not None:
            try:
                ov.hide_toast()
            except Exception:
                pass
        s = self.sess
        if s is None:
            return
        s.step = "export"
        s.handler = None
        s.refresh_export_defaults()
        self._loading = True
        self.name_in.setText(s.export_name)
        self.fmt.setCurrentText(s.export_format)
        self.add_map.setChecked(s.export_add_to_map)
        self._loading = False
        self.export_err.hide()
        ov = self.overlays()
        ov.set()
        c = s.counts()
        self.summary.setText(f"{_plural(c['lines'], 'line', 'lines')} · {length_text(c['km'])}")
        self.summary_tiles.setText(_tiles_caption(s))
        self.lengths.set_values({cid: (km if s.class_on.get(cid) else 0.0) for cid, km in c["km_by_class"].items()})
        self.export_btn.setText(f"Export {_plural(c['lines'], 'line', 'lines')}")
        self.export_btn.setEnabled(c["lines"] > 0)
        self._refresh_target()
        s.zoom_to_area()

    def do_export(self) -> None:
        s = self.sess
        if s is None or getattr(self, "_exporting", False):
            return
        s.export_name = self.name_in.text().strip() or s.export_name
        s.export_format = self.fmt.currentText()
        s.export_add_to_map = self.add_map.isChecked()
        self._folder_shown(self.folder_in.text())
        self.export_err.hide()
        self._exporting = True
        try:
            info = finish_review(self.ctx, s)
        except export_mod.ExportError as e:
            self._export_failed(e.user_text, f"{e.kind}: {e.detail}")
            return
        except Exception as e:
            self._export_failed(export_mod.user_text(export_mod.UNKNOWN, s.export_folder),
                                f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            return
        finally:
            self._exporting = False
        if info is not None:
            self.ctx.go("RV-Recap")

    def _export_failed(self, text: str, detail: str) -> None:
        """One plain sentence on the card; the driver's or Python's own words go to the message log only.
        The review, the card and Browse… stay as they are, so the user can pick another folder and press Export again."""
        _log(f"Export to {self.sess.export_folder if self.sess else ''} failed: {detail}")
        self.export_err.setText(text)
        self.export_err.show()


def _log(msg: str) -> None:
    from .controller import log

    log(msg, Qgis.MessageLevel.Warning)


def finish_review(ctx, s: ReviewSession) -> Dict[str, Any]:
    """Export with the settings as they stand, end the review, remember the result for the recap.  Only the write can
    fail the export (``ExportError``: the review stays); once the file is written the recap follows whatever else
    happens, so a hiccup after the write never asks for an export that already happened."""
    info = s.export()
    try:
        run = getattr(ctx, "run", None)
        info["tiles_used"] = s.tiles_charged
        info["tiles_held"] = s.tiles_held if getattr(s, "partial", False) else None
        info["tiles_planned"] = s.tiles_planned
        info["tiles_ran"] = s.tiles_ran
        info["stopped"] = s.stopped
        info["run_id"] = s.run_id
        info["imagery"] = (s.summary.get("imagery") or {}).get("name") or (s.ref.layer_name or "")
        img = getattr(run, "imagery", None) if run is not None else None
        info["imagery_id"] = getattr(img, "layer_id", "") or ""
        info["imagery_source"] = getattr(img, "source", "") or ""
        if getattr(img, "name", ""):
            info["imagery"] = img.name
    except Exception:
        _log(f"Export: the recap details were not all read: {traceback.format_exc()}")
    info.pop("layer", None)
    ctx.extras["last_export"] = info
    for step in (lambda: teardown(ctx), ctx.refresh_account):
        try:
            step()
        except Exception:
            _log(f"Export: after the file was written: {traceback.format_exc()}")
    return info


@registry.dialog("RV-Exit")
def open_exit(ctx, **kw):
    s = _session(ctx)
    if s is None:
        return None
    return ExitDialog(ctx, s, ctx.panel)


class ExitDialog(QDialog):
    def __init__(self, ctx, s: ReviewSession, parent=None):
        super().__init__(parent)
        self.ctx, self.s = ctx, s
        self.setWindowTitle("Road Extractor")
        self.setObjectName("flowDialog")
        card_dialog(self)
        self.setStyleSheet(theme.base_qss() + f"QDialog#flowDialog{{background:{theme.BG_DIALOG};"
                                              f"border:1px solid {theme.HAIRLINE};border-radius:14px;}}")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setModal(True)
        self.setMinimumSize(520, 360)
        lay = vbox(self, margins=(0, 0, 0, 0), spacing=0)
        head = hbox(margins=(24, 20, 20, 0), spacing=10)
        col = vbox(spacing=6)
        col.addWidget(caps(label("Review · Exit", mono=True, name="exitEyebrow", wrap=False)))
        h = label("Leave without exporting?", name="exitHeading", wrap=False)
        h.setStyleSheet("font-size:20px;font-weight:400;")
        col.addWidget(h)
        head.addLayout(col, 1)
        self.x_btn = button("", name="closeButton")
        self.x_btn.setProperty("icon", True)
        self.x_btn.setIcon(icons.icon("close", 16, theme.TEXT_2, 1.6))
        self.x_btn.setIconSize(QSize(16, 16))
        self.x_btn.setStyleSheet("QPushButton{border:none;background:transparent;border-radius:8px;}")
        self.x_btn.setAccessibleName("Close")
        self.x_btn.clicked.connect(self.cancel)
        head.addWidget(self.x_btn, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)
        body = vbox(margins=(24, 16, 24, 0), spacing=12)
        c = s.counts()
        mono = "<span style=\"font-family:'IBM Plex Mono'\">{}</span>"
        km_txt = length_text(c["km"])
        self.summary = QLabel(f"You have {mono.format(count(c['lines']))} {'line' if c['lines'] == 1 else 'lines'} · "
                              f"{mono.format(km_txt)} that "
                              f"{'is' if c['lines'] == 1 else 'are'} not exported yet.")
        self.summary.setObjectName("exitSummary")
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"font-size:14px;color:{theme.TEXT};")
        body.addWidget(self.summary)
        warn = QFrame()
        warn.setObjectName("exitLostBox")
        warn.setStyleSheet("QFrame#exitLostBox{background:rgba(245,158,11,0.10);border:1px solid rgba(245,158,11,0.3);"
                           "border-radius:10px;}")
        wl = hbox(warn, margins=(12, 10, 12, 10), spacing=10)
        wl.addWidget(_icon_label("warning-triangle", 15, theme.WARNING), 0, Qt.AlignmentFlag.AlignTop)
        lost = label("Lines you do not export are lost. They cannot be brought back.", name="exitLost")
        lost.setStyleSheet(f"font-size:13px;color:{theme.WARNING};background:transparent;border:none;")
        wl.addWidget(lost, 1)
        body.addWidget(warn)
        lay.addLayout(body)
        lay.addStretch(1)
        self.error = label("", danger=True, name="exitError")
        self.error.hide()
        eb = vbox(margins=(24, 8, 24, 0))
        eb.addWidget(self.error)
        lay.addLayout(eb)
        lay.addWidget(hairline())
        foot = hbox(margins=(24, 16, 24, 20), spacing=10)
        self.discard_btn = button("Discard and exit", outline_danger=True, name="discardButton")
        self.keep_btn = button("Keep reviewing", name="keepButton")
        self.export_btn = button("Export first", primary=True, name="exportFirstButton")
        self.discard_btn.setStyleSheet("QPushButton{min-height:38px;padding:0 18px;font-size:14px;font-weight:500;}")
        self.keep_btn.setStyleSheet(f"QPushButton{{min-height:38px;padding:0 18px;font-size:14px;font-weight:500;"
                                    f"background:{theme.CARD_BG};border:1px solid {theme.BORDER_STRONG};}}")
        self.export_btn.setStyleSheet("QPushButton{min-height:40px;padding:0 22px;font-size:14px;font-weight:500;}")
        self.export_btn.setDefault(True)
        self.discard_btn.clicked.connect(self.discard)
        self.keep_btn.clicked.connect(self.cancel)
        self.export_btn.clicked.connect(self.export_first)
        foot.addWidget(self.discard_btn)
        foot.addStretch(1)
        foot.addWidget(self.keep_btn)
        foot.addWidget(self.export_btn)
        lay.addLayout(foot)

    def reject(self) -> None:
        self.cancel()

    def cancel(self) -> None:
        """Keep reviewing: close the dialog, the review stays where it was (a quit, New or Open asked for is dropped)."""
        self.ctx.after_leave = None
        self.ctx.close_dialog("RV-Exit")

    def export_first(self) -> None:
        self.ctx.after_leave = None
        self.ctx.close_dialog("RV-Exit")
        if self.ctx.review is not None:
            self.ctx.go("RV-Export")

    def discard(self) -> None:
        after, self.ctx.after_leave = getattr(self.ctx, "after_leave", None), None
        self.ctx.close_dialog("RV-Exit")
        teardown(self.ctx)
        self.ctx.go("EX-Start")
        if after is not None:
            QTimer.singleShot(0, after)


class _TilesLine(QWidget):
    """The Recap tiles card body: "This run: 36 tiles" left, "176 left this month" right (figures in mono), and
    the 6 px bar of this run's share of the month's tiles.  ``text()`` is the whole line."""

    def __init__(self):
        super().__init__()
        self.setObjectName("tilesLine")
        self._text = ""
        self.note = QLabel()
        self.note.setObjectName("tilesNote")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"font-size:12px;color:{theme.WARNING};")
        self.note.hide()
        col = vbox(self, spacing=6)
        row = hbox(spacing=8)
        self.run_lb = MonoNumberLabel("", wrap=False)
        self.left_lb = MonoNumberLabel("", wrap=False)
        for lb in (self.run_lb, self.left_lb):
            lb.setStyleSheet(f"font-size:12px;color:{theme.TEXT_2};")
        row.addWidget(self.run_lb, 1)
        row.addWidget(self.left_lb)
        col.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        self.bar.setStyleSheet(f"QProgressBar{{background:rgba(247,245,251,0.08);border:none;border-radius:3px;"
                               f"min-height:6px;max-height:6px;}}QProgressBar::chunk{{background:{theme.ACCENT};"
                               f"border-radius:3px;}}")
        col.addWidget(self.bar)
        col.addWidget(self.note)

    def text(self) -> str:
        return self._text

    def set_values(self, used: int, left: Optional[int], note: str = "") -> None:
        self.note.setText(note)
        self.note.setVisible(bool(note))
        run = f"This run: {_plural(used, 'tile', 'tiles')} charged"
        self.run_lb.setText(run)
        self.left_lb.setText("" if left is None else f"{count(left)} left this month")
        self._text = run if left is None else f"{run} · {count(left)} left this month"
        total = used + (left or 0)
        self.bar.setValue(int(1000 * used / total) if total else 0)
        self.bar.setVisible(left is not None)
        self.left_lb.setStyleSheet(f"font-size:12px;color:{theme.WARNING if left == 0 else theme.TEXT_2};")


@registry.screen("RV-Recap")
class RvRecap(Screen):
    def __init__(self, ctx):
        super().__init__(ctx)
        sc = FitScrollArea()
        sc.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("recapBody")
        sc.setStyleSheet(f"QScrollArea{{background:{theme.BG_COVER};border:none;}}"
                         f"QWidget#recapBody{{background:{theme.BG_COVER};}}" + _CONTROLS_QSS + SCROLLBAR_QSS)
        sc.viewport().setObjectName("reviewViewport")
        sc.viewport().setStyleSheet(f"QWidget#reviewViewport{{background:{theme.BG_COVER};}}")
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = vbox(inner, margins=(14, 14, 14, 16), spacing=10)
        sc.setWidget(inner)
        vbox(self, spacing=0).addWidget(sc)
        col = vbox(spacing=6)
        col.addWidget(caps(label("Imagery", mono=True, wrap=False, name="imageryLabel")))
        self.imagery = QComboBox()
        self.imagery.setObjectName("imageryCombo")
        name_combo(self.imagery, "Imagery")
        self.imagery.setFixedHeight(40)
        self.imagery.setStyleSheet(f"QComboBox{{background:#1a1b20;border:1px solid {theme.BORDER_STRONG};"
                                   f"border-radius:8px;padding:0 12px;font-size:13px;font-weight:{theme.LIGHT};}}")
        style_combo_popup(self.imagery)
        col.addWidget(self.imagery)
        self.imagery.activated.connect(self._pick_imagery)
        self.body.addLayout(col)
        self.new_btn = primary_pill("New area", "newAreaButton")
        self.new_btn.installEventFilter(_NoDoubleClick(self.new_btn))
        self.new_btn.clicked.connect(self._new_area)
        self._watching = False
        self.body.addWidget(self.new_btn)
        self.banner = QFrame()
        self.banner.setObjectName("savedBanner")
        self.banner.setStyleSheet("QFrame#savedBanner{background:rgba(112,223,173,0.10);border:1px solid rgba(112,223,173,0.3);"
                                  "border-radius:10px;}")
        bl = hbox(self.banner, margins=(12, 10, 12, 10), spacing=8)
        tick = _icon_label("check-tight", 15, theme.SUCCESS, 2.0)
        bl.addWidget(tick, 0, Qt.AlignmentFlag.AlignTop)
        txt = vbox(spacing=3)
        self.saved_text = label("", name="savedText")
        self.saved_text.setStyleSheet(f"color:{theme.SUCCESS};font-weight:500;")
        self.saved_text.setWordWrap(True)
        self.saved_path = label("", name="savedPath")
        self.saved_path.setStyleSheet(f"color:{theme.TEXT};font-family:{theme.FONT_MONO};font-size:11px;")
        self.saved_path.setWordWrap(True)
        self.saved_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layer_row = hbox(spacing=4)
        self.layer_prefix = label("Layer", name="layerPrefix", wrap=False)
        self.layer_link = button("", link=True, name="layerLink")
        self.layer_link.setStyleSheet(f"QPushButton{{border:none;color:{theme.SUCCESS};text-decoration:underline;"
                                      "font-weight:500;padding:0;}")
        set_tip(self.layer_link, "Zoom the map to this layer")
        self.layer_link.clicked.connect(self._zoom)
        self.layer_tail = label("added to map", name="layerTail", wrap=False)
        for w in (self.layer_prefix, self.layer_tail):
            w.setStyleSheet(f"color:{theme.TEXT};font-size:13px;font-weight:400;")
        layer_row.addWidget(self.layer_prefix)
        layer_row.addWidget(self.layer_link)
        layer_row.addWidget(self.layer_tail)
        layer_row.addStretch(1)
        self.layer_row = QWidget()
        self.layer_row.setObjectName("layerRow")
        self.layer_row.setLayout(layer_row)
        txt.addWidget(self.saved_text)
        txt.addWidget(self.saved_path)
        txt.addWidget(self.layer_row)
        bl.addLayout(txt, 1)
        self.body.addWidget(self.banner)
        self.lengths = LengthByClass("lengthCard")
        self.body.addWidget(self.lengths)
        t = card(name="tilesCard")
        tl = vbox(t, margins=(14, 14, 14, 14), spacing=8)
        tl.addWidget(_card_title("Tiles"))
        self.tiles_line = _TilesLine()
        tl.addWidget(self.tiles_line)
        self.body.addWidget(t)
        self.body.addStretch(1)
        ctx.account_changed.connect(self._tiles)

    def _new_area(self) -> None:
        self.ctx.area = None
        lid = self.imagery.currentData()
        lyr = QgsProject.instance().mapLayer(lid) if lid else None
        if not isinstance(lyr, QgsRasterLayer):
            from . import extract

            extract.refuse_stale_imagery(self.ctx)
            return
        self._pick_imagery()
        if getattr(self.ctx.imagery, "layer_id", None) != lyr.id():
            from . import extract

            extract.refuse_stale_imagery(self.ctx)
            return
        self.ctx.go("EX-DrawArea")

    def _layers_changed(self, *_a) -> None:
        """Rasters were added to or removed from the project while the recap is open."""
        if self.ctx.current_board == "RV-Recap":
            self._fill_imagery()
            self._fill_saved(self.ctx.extras.get("last_export") or {})

    def on_show(self, **kw) -> None:
        info = self.ctx.extras.get("last_export") or {}
        from . import extract

        if self.ctx.imagery is not None and not extract.imagery_in_project(self.ctx):
            self.ctx.imagery = self.ctx.area = None
        ov = getattr(self.ctx, "_review_overlays", None)
        if ov is not None:
            ov.clear()
        if not self._watching:
            self._watching = True
            prj = QgsProject.instance()
            prj.layersAdded.connect(self._layers_changed)
            prj.layersRemoved.connect(self._layers_changed)
            self.destroyed.connect(lambda *_: self._unwatch(prj))
        self._fill_imagery()
        self._fill_saved(info)

    def _unwatch(self, prj) -> None:
        for sig in (prj.layersAdded, prj.layersRemoved):
            try:
                sig.disconnect(self._layers_changed)
            except (TypeError, RuntimeError):
                pass

    def _fill_imagery(self) -> None:
        info = self.ctx.extras.get("last_export") or {}
        self.imagery.blockSignals(True)
        self.imagery.clear()
        want = info.get("imagery", "")
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsRasterLayer) and lyr.providerType() == "gdal":
                self.imagery.addItem(lyr.name(), lyr.id())
        cur = getattr(self.ctx, "imagery", None)
        pick = -1
        for j in range(self.imagery.count()):
            lyr = QgsProject.instance().mapLayer(self.imagery.itemData(j))
            if lyr is None:
                continue
            if (cur is not None and self.imagery.itemData(j) == getattr(cur, "layer_id", None)) or \
                    (cur is None and info.get("imagery_id") and self.imagery.itemData(j) == info["imagery_id"]):
                pick = j
                break
        if pick < 0:
            src = (getattr(cur, "source", "") or info.get("imagery_source", "")).split("|")[0]
            nm = getattr(cur, "name", "") or want
            for j in range(self.imagery.count()):
                lyr = QgsProject.instance().mapLayer(self.imagery.itemData(j))
                if lyr is not None and ((src and lyr.source().split("|")[0] == src) or lyr.name() == nm):
                    pick = j
                    break
        if pick >= 0:
            self.imagery.setCurrentIndex(pick)
        if self.imagery.count() == 0:
            self.imagery.addItem("No raster in this project")
            self.imagery.setCurrentIndex(0)
        self.imagery.blockSignals(False)

    def _fill_saved(self, info) -> None:
        n = info.get("lines", 0)
        self.saved_text.setText(f"{_plural(n, 'line', 'lines')} · {length_text(info.get('length_km', 0))} exported")
        self.saved_path.setText(info.get("path", ""))
        self.saved_path.setToolTip(info.get("path", ""))
        added = bool(info.get("layer_added", info.get("layer_id")))
        self.layer_row.setVisible(added)
        here = QgsProject.instance().mapLayer(info.get("layer_id", "") or "") is not None or not info.get("layer_id")
        self.layer_link.setText(info.get("name", ""))
        self.layer_link.setEnabled(here)
        self.layer_link.setStyleSheet(
            f"QPushButton{{border:none;color:{theme.SUCCESS if here else theme.TEXT};"
            f"text-decoration:{'underline' if here else 'none'};font-weight:{500 if here else 400};padding:0;}}")
        self.layer_tail.setText("added to map" if here else "removed from the map")
        self.lengths.set_values(info.get("by_class_km") or {})
        self._tiles()

    def _pick_imagery(self, _i: int = 0) -> None:
        """A different raster picked here is the one New area uses."""
        lid = self.imagery.currentData()
        lyr = QgsProject.instance().mapLayer(lid) if lid else None
        if not isinstance(lyr, QgsRasterLayer):
            return
        from . import extract

        info = extract.inspect_layer(lyr)
        if info.usable:
            if self.ctx.extras.get("layer_id") != lyr.id():
                self.ctx.area = None
            self.ctx.extras["layer_id"] = lyr.id()
            self.ctx.imagery = info
            from .extract import zoom_to_imagery

            zoom_to_imagery(self.ctx, info)

    def _tiles(self) -> None:
        info = self.ctx.extras.get("last_export") or {}
        used = int(info.get("tiles_used", 0))
        left = self.ctx.tiles_left()
        note = ""
        if info.get("tiles_held") is not None:
            from .extract import partial_text

            note = partial_text(used, int(info["tiles_held"]))
        elif info.get("stopped"):
            note = stopped_note(used, int(info.get("tiles_planned", 0) or 0))
        self.tiles_line.set_values(used, left, note)

    def _zoom(self) -> None:
        from . import crs_util

        info = self.ctx.extras.get("last_export") or {}
        lyr = QgsProject.instance().mapLayer(info.get("layer_id", ""))
        if lyr is None:
            return
        crs_util.zoom_to_rect(self.ctx.iface.mapCanvas(), lyr.extent(), lyr.crs(), 1.1)
