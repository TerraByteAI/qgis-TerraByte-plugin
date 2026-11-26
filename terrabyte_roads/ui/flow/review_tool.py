"""Map tool for the review: hover highlights a whole line, click selects it, Delete / Backspace delete it,
Ctrl+Z / Cmd+Z undo, Esc deselects, a click on empty map clears the selection, a drag pans.

Keys are claimed with one event filter that exists only while this tool is the canvas tool.  It is installed on the
canvas, on the Road Extractor panel and on the widget that has the keyboard focus when that is the map or in the panel,
never on the application: an application-wide Python filter is handed every event of every object, and can crash
inside the sip conversion of an object that is being created or destroyed.  A key event Qt sends to a child of the
canvas or of the panel that the child does not use (a button does not use Delete) goes on to the parent, so the
filter on the canvas and on the panel sees it whatever widget in them has the focus.  The filter takes
ShortcutOverride (so QGIS-wide shortcuts on those keys, such as its own undo or "Delete selected", never fire) and the
key press itself, for a key aimed at the map canvas or at the Road Extractor panel (a text box keeps its own
Backspace).  Nothing is passed on to QGIS, so it can't say "No vector layer selected".

Qt 6 can eat a key press before any widget sees it.  On every key press Qt first asks its shortcut map; once that
map is in the middle of a key sequence ("partial match"), the next press that completes nothing is swallowed whole:
no ShortcutOverride, no KeyPress, only the key release reaches the widget.  Qt 6 starts such a sequence on a key event
that has no key code (key 0): an empty key sequence is the start of every shortcut.  Windows sends one, the
unassigned virtual key 0xE8, whenever an input tool brings a window to the front or masks the Alt key (such as
AutoHotkey's menu mask key).  Qt 5 drops that event first.  Two rules make the keys deterministic on both:

* the ShortcutOverride of a key event with no key code and no text is taken while the review has the keys, so it never
  starts a sequence that would swallow the next key;
* a release of Delete, Backspace or Ctrl+Z whose press never reached the review (the map was already in a sequence, or
  anything else ate it) does what the press would have done, unless the widget with the focus uses those keys itself
  (a text box: it had the press).  A press the review did see is never acted on twice.  Not Esc: its press may have
  closed a dialog or a menu before the release reached the map.
"""
from __future__ import annotations

import sys
import time
import weakref
from typing import Dict, Optional

from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import QEvent, QObject, QPoint, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QApplication, QComboBox,
                                 QLineEdit, QPlainTextEdit, QTextEdit, QWidget)

KEY = Qt.Key
MOD = Qt.KeyboardModifier
_OTHER = {KEY.Key_Delete: "delete", KEY.Key_Backspace: "delete", KEY.Key_Escape: "deselect"}
CLAIMED = set(_OTHER) | {KEY.Key_Z}
MAC = sys.platform == "darwin"
DRAG_PX = 4
PRESS_KEPT_S = 10.0
_SO, _KP, _KR = QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease
LATE = ("delete", "undo")


def _plain(modifiers) -> bool:
    """True when no Ctrl, Alt or Meta is held (those stay with QGIS)."""
    mask = MOD.ControlModifier | MOD.AltModifier | MOD.MetaModifier
    return not (modifiers & mask)


def key_action(key: int, modifiers) -> Optional[str]:
    """The review action for a key press, or None when the key is not ours.

    Delete and Backspace delete, Esc deselects, Ctrl+Z (Cmd+Z on a Mac, which Qt reports as Control) undoes."""
    if key == KEY.Key_Z:
        only = modifiers & (MOD.ControlModifier | MOD.AltModifier | MOD.MetaModifier | MOD.ShiftModifier)
        return "undo" if only == MOD.ControlModifier else None
    if not _plain(modifiers) or modifiers & MOD.ShiftModifier:
        return None
    return _OTHER.get(key)


def _is_text_entry(w) -> bool:
    if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
        return True
    if isinstance(w, QComboBox) and w.isEditable():
        return True
    return False


def _uses_keys(w) -> bool:
    """A widget that takes Delete / Backspace for itself (a text box, a list, a drop-down's type-to-find, a slider): a
    release seen without its press there is that widget's, never the review's."""
    return _is_text_entry(w) or isinstance(w, (QComboBox, QAbstractItemView, QAbstractSlider))


def nameless(ev) -> bool:
    """A key event Qt has no key for (Windows' unassigned virtual key 0xE8 and the like): key 0 or Key_unknown, no text."""
    try:
        return ev.key() in (0, KEY.Key_unknown) and not ev.text()
    except (RuntimeError, AttributeError):
        return False


class _Filter(QObject):
    def __init__(self, tool: "ReviewTool"):
        super().__init__()
        self.tool = tool
        self._down: Dict[int, float] = {}
        self.late = 0

    def _ours(self, obj) -> bool:
        """The key is aimed at the map or at the panel (and not at a text box)."""
        if not isinstance(obj, QWidget) or _is_text_entry(obj):
            return False
        t = self.tool
        try:
            cv = t.canvas()
            if obj is cv or cv.isAncestorOf(obj):
                return True
            panel = getattr(t.session.ctx, "panel", None)
            return panel is not None and (obj is panel or panel.isAncestorOf(obj))
        except RuntimeError:
            return False

    def _focus(self, obj):
        """The widget the key went to first: the application's focus widget, or (QGIS not the active window) the
        focus widget of ``obj``'s window."""
        fw = QApplication.focusWidget()
        if fw is None and isinstance(obj, QWidget):
            try:
                fw = obj.window().focusWidget()
            except RuntimeError:
                fw = None
        return fw

    def eventFilter(self, obj, ev):
        typ = ev.type()
        if typ not in (_SO, _KP, _KR):
            return False
        try:
            sess = self.tool.session
            if sess.closed or not sess.keys_wanted:
                return False
            if typ != _KR and nameless(ev):
                if not self._ours(obj):
                    return False
                ev.accept()
                return True
            act = key_action(ev.key(), ev.modifiers())
            if typ == _KR:
                return self._released(obj, ev, act)
            if not act or not self._ours(obj):
                return False
            if typ == _SO:
                ev.accept()
                return True
            self._down[int(ev.key())] = time.monotonic()
            if ev.isAutoRepeat() and act == "undo":
                return True
            sess.rearm()
            self.tool.dispatch(act)
            ev.accept()
            return True
        except RuntimeError:
            return False

    def _released(self, obj, ev, act: Optional[str]) -> bool:
        """The release of a key whose press reached the review only clears it; a release without its press does
        what the press would have done (the press was eaten before any widget saw it)."""
        key = int(ev.key())
        if key not in CLAIMED or not self._ours(obj):
            return False
        if ev.isAutoRepeat():
            return act is not None
        seen = self._down.pop(key, None)
        if seen is not None and time.monotonic() - seen < PRESS_KEPT_S:
            ev.accept()
            return True
        if act not in LATE:
            return False
        fw = self._focus(obj)
        if fw is None or _uses_keys(fw) or not self._ours(fw):
            return False
        sess = self.tool.session
        if act == "undo" and not sess.undo_is_ours():
            ev.accept()
            return True
        self.late += 1
        sess.rearm()
        self.tool.dispatch(act)
        ev.accept()
        return True


class ReviewTool(QgsMapTool):
    """Review keys, hover, click-to-pick and pan.  Actions go to the session (``session.handle_action``)."""

    action = pyqtSignal(str)

    _trb = True

    def __init__(self, canvas, session):
        super().__init__(canvas)
        self.session = session
        self._filter = _Filter(self)
        self._installed = False
        self._targets = []
        self._press: Optional[QPoint] = None
        self._dragging = False

    def activate(self) -> None:
        super().activate()
        if not self._installed:
            app = QApplication.instance()
            if app is not None:
                app.focusChanged.connect(self._focus_changed)
                self._installed = True
        self._watch(self.canvas())
        panel = getattr(self.session.ctx, "panel", None)
        self._watch(panel)
        fw = QApplication.focusWidget()
        if not (panel is not None and isinstance(fw, QWidget) and (fw is panel or panel.isAncestorOf(fw))):
            self.canvas().setFocus()
        self._watch(QApplication.focusWidget())
        self._emit_state(True)

    def deactivate(self) -> None:
        super().deactivate()
        try:
            wanted = self.session.keys_wanted and not self.session.closed
        except RuntimeError:
            wanted = False
        if not wanted:
            self.release()
        else:
            try:
                self.session.deselect()
                self.session.hover_line(None)
            except RuntimeError:
                pass
        self._press, self._dragging = None, False
        self._emit_state(False)

    def _emit_state(self, active: bool) -> None:
        """The session can already be gone when QGIS deactivates the tool (panel or project closed mid-review)."""
        try:
            self.session.tool_state.emit(active)
        except RuntimeError:
            pass

    def _focus_changed(self, _old, new) -> None:
        self._watch(new)
        try:
            panel = getattr(self.session.ctx, "panel", None)
            if panel is not None and isinstance(new, QWidget) and (new is panel or panel.isAncestorOf(new)):
                self.session.rearm()
        except RuntimeError:
            pass

    def _watch(self, w) -> None:
        """Put the filter on ``w`` (the canvas, the panel, the focus widget) when it is the map or in the panel, and
        leave the others."""
        if w is None or not isinstance(w, QWidget):
            return
        try:
            if any(r() is w for r in self._targets):
                return
            if not self._filter._ours(w):
                return
            w.installEventFilter(self._filter)
            w.setProperty("_trb_key_watched", True)
            self._targets.append(weakref.ref(w))
        except RuntimeError:
            pass

    def release(self) -> None:
        if self._installed:
            app = QApplication.instance()
            try:
                if app is not None:
                    app.focusChanged.disconnect(self._focus_changed)
            except (RuntimeError, TypeError):
                pass
            self._installed = False
        targets, self._targets = self._targets, []
        for ref in targets:
            w = ref()
            if w is None:
                continue
            try:
                w.removeEventFilter(self._filter)
                w.setProperty("_trb_key_watched", None)
            except RuntimeError:
                pass

    def is_active(self) -> bool:
        return self.canvas().mapTool() is self

    def dispatch(self, name: str) -> None:
        try:
            self.action.emit(name)
            self.session.handle_action(name)
        except RuntimeError:
            return

    def keyPressEvent(self, e) -> None:
        act = key_action(e.key(), e.modifiers())
        if act is None:
            e.ignore()
            return
        e.accept()
        self.dispatch(act)

    def canvasPressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._press, self._dragging = QPoint(e.pos()), False

    def canvasMoveEvent(self, e) -> None:
        if self._press is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            if not self._dragging and (e.pos() - self._press).manhattanLength() >= DRAG_PX:
                self._dragging = True
            if self._dragging:
                self.canvas().panAction(e)
            return
        try:
            lid = self.session.pick_at(self.toMapCoordinates(e.pos()))
            self.session.hover_line(lid)
            self.canvas().setCursor(Qt.CursorShape.PointingHandCursor if lid is not None
                                    else Qt.CursorShape.ArrowCursor)
        except RuntimeError:
            return

    def canvasReleaseEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        was_drag, self._press, self._dragging = self._dragging, None, False
        if was_drag:
            self.canvas().panActionEnd(e.pos())
            return
        try:
            lid = self.session.pick_at(self.toMapCoordinates(e.pos()))
            self.session.select_line(lid)
        except RuntimeError:
            return
        self.canvas().setFocus()
