"""The Road Extractor dock panel: header (logo, title, gear, close) + a stack of screens, and the click guard that keeps
a click meant for one screen from landing on the next."""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QObject, QTimer, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QGuiApplication, QIcon, QPainter
from qgis.PyQt.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QComboBox,
                                 QDialog, QDockWidget, QFrame, QLabel, QLineEdit, QPushButton, QStackedWidget, QWidget)

from ...core import paths
from .. import icons, theme
from ..widgets import DIALOG_HOOKS, Screen, button, hbox, label, track, vbox
from . import registry
from .brand import logo_pixmap

HOLD_MS = {"EX-Setup": 600, "EX-Running": 600, "EX-Failed": 600, "EN-BeforeStart": 600}
GUARD_SPAN_S = 30.0
OFFSET_DECAY_MS_PER_S = 1.0
TRUST_MIN_MS = 150.0
QUEUE_MAX_MS = 5000.0
_WRAP = 1 << 32
_HALF = 1 << 31
WATCHED = "_trb_click_guard"

_MOUSE = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick)
_KEYS = (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
_INPUT = frozenset(_MOUSE + _KEYS + (QEvent.Type.MouseMove,))
_ACTIVATE = frozenset((Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select))
_MOVE, _DBL = QEvent.Type.MouseMove, QEvent.Type.MouseButtonDblClick
_SHOW, _ACTION_ADDED = QEvent.Type.Show, QEvent.Type.ActionAdded
_TAKES_INPUT = (QAbstractButton, QComboBox, QAbstractSpinBox, QAbstractSlider, QLineEdit, QAbstractItemView)


def _takes_input(w: QWidget) -> bool:
    return isinstance(w, _TAKES_INPUT) or w.focusPolicy() != Qt.FocusPolicy.NoFocus


class ClickGuard(QObject):
    """The click guard for every panel screen and every flow dialog.

    It is an event filter on the plugin's own widgets only, never on the QApplication.  A Python filter on the
    application makes sip build a Python object for the receiver of every event in QGIS (timers of objects that are
    not the plugin's included) before a line of ``eventFilter`` runs; on macOS that conversion can crash QGIS shortly
    after start.  The guard sits on the panel, each screen, each flow dialog and the TerraByte menu (the roots: their
    Show and ActionAdded events) and on every widget in them that takes input (buttons, boxes, lists: their presses,
    releases and keys).  ``watch`` adds a root and what is in it now; a screen or dialog is watched again whenever it is
    stamped, so widgets made later are covered too.  ``dispose`` takes it off every widget.

    Input events carry the window system's time (``QInputEvent.timestamp()``, milliseconds), which is not
    ``time.monotonic()``.  An event is never delivered before it was made, so every input event the guard gets gives a
    lower bound of (event clock - monotonic clock); the largest one seen (sinking by at most OFFSET_DECAY_MS_PER_S)
    places the moment a screen or dialog appeared on the event clock (``stamp``).  Then, for a mouse press, double-click
    or release and for Space / Enter / Return / Select on a widget of that screen or dialog:

    * an event made before it appeared (by at most QUEUE_MAX_MS) is dropped, however late a busy GUI thread delivers it
      (a press accepted on the old screen whose release lands on the new one, a click queued while the panel was busy);
    * a double-click whose second press lands on it is dropped when that press came within the double-click interval
      of it appearing (the first press belonged to the screen before: a double-click on Next never presses "Extract",
      while its first click still goes to the run check);
    * an auto-repeated activation key is dropped (a key held down since the screen before);
    * on the screens in HOLD_MS everything is dropped for that long after they appeared.

    The double-click, auto-repeat and HOLD_MS rules are also applied by delivery time (monotonic), which needs no event
    clock.  ``QAbstractButton.click()`` and accessibility actions carry no event, and an event without a time is not
    judged.  The event-time rules apply only once the event clock has been seen to run in step with the monotonic one
    (a synthetic event carrying a plain counter is not trusted), only to events that are at most QUEUE_MAX_MS older
    than the screen (an event from another clock is not judged), and every rule only for GUARD_SPAN_S after a screen
    appeared, so a wrong clock can never lock the panel."""

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._off: Optional[float] = None
        self._off_m = 0.0
        self._ref = None
        self.trusted = False
        self._stamps: Dict[int, List[Any]] = {}
        self.owned: List[Any] = []
        self._watched: List[Any] = []
        self.installed = False
        self.dropped = 0
        self.trace: Deque[tuple] = deque(maxlen=64)
        try:
            self._dci = int(QGuiApplication.styleHints().mouseDoubleClickInterval()) or 400
        except Exception:
            self._dci = 400

    def install(self) -> None:
        """Watch the owned roots; flow dialogs made from now on are watched when they are made (``card_dialog``)."""
        if self.installed:
            return
        self.installed = True
        if self._watch_dialog not in DIALOG_HOOKS:
            DIALOG_HOOKS.append(self._watch_dialog)
        for o in self.owned:
            self.watch(o)

    def dispose(self) -> None:
        self.installed = False
        try:
            DIALOG_HOOKS.remove(self._watch_dialog)
        except ValueError:
            pass
        watched, self._watched = self._watched, []
        for w in watched:
            try:
                if not sip.isdeleted(w):
                    w.removeEventFilter(self)
                    w.setProperty(WATCHED, None)
            except (RuntimeError, TypeError):
                pass
        self._stamps.clear()
        self.owned = []

    def watch(self, root) -> None:
        """Put the guard on ``root`` (a widget the plugin made: the panel, a screen, a flow dialog, the menu) and on
        every widget in it that takes input."""
        if not self.installed or root is None or not isinstance(root, QWidget):
            return
        try:
            if sip.isdeleted(root):
                return
            found = [w for w in root.findChildren(QWidget) if _takes_input(w)]
        except (RuntimeError, TypeError):
            return
        self._watched = [w for w in self._watched if not sip.isdeleted(w)]
        for w in [root] + found:
            try:
                if w.property(WATCHED):
                    continue
                w.installEventFilter(self)
                w.setProperty(WATCHED, True)
                self._watched.append(w)
            except (RuntimeError, TypeError):
                pass
        from ...plugin import no_menu_roles

        no_menu_roles(root)

    def _watch_dialog(self, dlg) -> None:
        self.watch(dlg)

    def _sample(self, ts: int, m: float) -> None:
        s = ts - m * 1000.0
        if self._off is None or s >= self._off - (m - self._off_m) * OFFSET_DECAY_MS_PER_S or s < self._off - _HALF:
            self._off, self._off_m = s, m
        if not self.trusted:
            r = self._ref
            if r is None:
                self._ref = (ts, m)
                return
            dm = (m - r[1]) * 1000.0
            if dm >= TRUST_MIN_MS:
                if abs((ts - r[0]) - dm) <= max(60.0, 0.2 * dm):
                    self.trusted = True
                self._ref = (ts, m)

    def clock(self, m: Optional[float] = None) -> Optional[float]:
        """Monotonic time ``m`` (now by default) on the event clock; None before any input event was seen."""
        if self._off is None:
            return None
        m = time.monotonic() if m is None else m
        return m * 1000.0 + self._off - max(0.0, m - self._off_m) * OFFSET_DECAY_MS_PER_S

    def stamp(self, widget: QWidget, board: str = "", hold_ms: Optional[int] = None) -> None:
        """``widget`` (a screen or a dialog) appeared just now."""
        if widget is None:
            return
        m = time.monotonic()
        hold = HOLD_MS.get(board, 0) if hold_ms is None else int(hold_ms)
        self._stamps[id(widget)] = [widget, self.clock(m), hold, m]
        self._prune()
        self.watch(widget)

    def stamp_of(self, widget: QWidget) -> Optional[float]:
        """When ``widget`` appeared, on the event clock (None: not stamped or no event seen yet)."""
        e = self._stamps.get(id(widget))
        if e is None:
            return None
        if e[1] is None:
            e[1] = self.clock(e[3])
        return e[1]

    def _prune(self) -> None:
        for k, e in list(self._stamps.items()):
            try:
                if sip.isdeleted(e[0]):
                    del self._stamps[k]
            except (RuntimeError, TypeError):
                self._stamps.pop(k, None)

    def _container(self, w: QWidget):
        for k, e in list(self._stamps.items()):
            c = e[0]
            try:
                if sip.isdeleted(c):
                    del self._stamps[k]
                    continue
                if (c is w or c.isAncestorOf(w)) and c.isVisible():
                    return e
            except (RuntimeError, TypeError):
                self._stamps.pop(k, None)
        return None

    def _judge(self, e, t, ev, ts: int, m: float) -> str:
        """Why the event is dropped ('' = taken)."""
        age_ms = (m - e[3]) * 1000.0
        if age_ms > GUARD_SPAN_S * 1000.0:
            return ""
        hold = e[2]
        if t == _DBL and age_ms < max(hold, self._dci):
            return "double-click"
        if t in _KEYS and ev.isAutoRepeat():
            return "auto-repeat"
        if age_ms < hold:
            return "hold"
        if not self.trusted:
            return ""
        if e[1] is None:
            e[1] = self.clock(e[3])
            if e[1] is None:
                return ""
        d = ts - e[1]
        if d < -_HALF:
            d += _WRAP
        elif d > _HALF:
            d -= _WRAP
        if d < -QUEUE_MAX_MS:
            return ""
        if t == _DBL and d < max(hold, self._dci):
            return "double-click (event time)"
        if d < 0:
            return "made before it appeared"
        return "hold (event time)" if d < hold else ""

    def eventFilter(self, obj, ev):
        try:
            t = ev.type()
            if t not in _INPUT:
                if t == _SHOW:
                    self._shown(obj)
                elif t == _ACTION_ADDED:
                    self._action_added(ev)
                return False
            ts = ev.timestamp()
            if not ts:
                return False
            m = time.monotonic()
            self._sample(ts, m)
            if t == _MOVE or not self._stamps:
                return False
            if t in _KEYS and ev.key() not in _ACTIVATE:
                return False
            e = self._container(obj)
            if e is None:
                return False
            why = self._judge(e, t, ev, ts, m)
            self.trace.append((int(t), ts, m, why))
            if not why:
                return False
            self.dropped += 1
            return True
        except Exception:
            return False

    def _shown(self, obj) -> None:
        """A flow dialog appeared (one not opened through ``ctx.go`` included): stamp it (which also gives its actions
        NoRole)."""
        if not isinstance(obj, QDialog):
            return
        old = self._stamps.get(id(obj))
        self.stamp(obj, hold_ms=old[2] if old is not None else 0)

    @staticmethod
    def _action_added(ev) -> None:
        """An action added later to the panel, a screen, the TerraByte menu or a flow dialog gets NoRole too
        (the guard sits only on the plugin's own widgets)."""
        from ...plugin import no_menu_role

        a = ev.action()
        if a is not None:
            no_menu_role(a)


class _Header(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.setObjectName("flowHeader")
        self.setFixedHeight(52)
        lay = hbox(self, margins=(16, 0, 10, 0), spacing=10)
        self.logo = QLabel()
        self.logo.setPixmap(logo_pixmap(20))
        self.logo.setFixedSize(20, 20)
        lay.addWidget(self.logo)
        col = vbox(spacing=3)
        self.title = label("Road Extractor", wrap=False, name="headerTitle")
        self.title.setStyleSheet("font-size:14px;font-weight:500;")
        track(self.title, -0.14)
        self.caption = label("TerraByte", mono=True, wrap=False, name="headerCaption")
        track(self.caption, 0.8, caps=True)
        self.caption.setStyleSheet(f"font-family:{theme.FONT_MONO};font-size:10px;color:{theme.TEXT_MUTED};")
        self.title.setFixedHeight(14)
        self.caption.setFixedHeight(10)
        col.setSpacing(2)
        col.addStretch(1)
        col.addWidget(self.title)
        col.addWidget(self.caption)
        col.addStretch(1)
        lay.addLayout(col, 1)
        self.gear = self._icon_button("gear", "settingsButton", "Settings")
        self.gear.clicked.connect(lambda: ctx.go("ST-Account"))
        lay.addWidget(self.gear)
        self.close_btn = self._icon_button("close", "closePanelButton", "Close panel")
        lay.addWidget(self.close_btn)

    @staticmethod
    def _icon_button(name: str, obj: str, tip: str) -> QPushButton:
        b = QPushButton()
        b.setObjectName(obj)
        b.setIcon(icons.icon(name, 16, theme.TEXT_2))
        b.setIconSize(QSize(16, 16))
        b.setFixedSize(32, 32)
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet("QPushButton{background:transparent;border:none;border-radius:8px;}"
                        "QPushButton:hover{background:rgba(247,245,251,0.06);}")
        return b

    def refresh(self, bare: bool) -> None:
        show = (not bare) and self.ctx.signed_in
        self.gear.setVisible(show)


class RoadExtractorPanel(QDockWidget):
    closed_by_user = pyqtSignal()

    def __init__(self, ctx, parent: Optional[QWidget] = None):
        super().__init__("Road Extractor", parent)
        self.setObjectName("TerraByteRoadExtractorPanel")
        self.ctx = ctx
        ctx.panel = self
        self.setStyleSheet(theme.base_qss())
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                         | QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.setMinimumWidth(400)
        root = QWidget()
        root.setObjectName("flowRoot")
        self.setWidget(root)
        self.setTitleBarWidget(QWidget())
        lay = vbox(root, spacing=0)
        self.header = _Header(ctx)
        self.header.close_btn.clicked.connect(self.close_panel)
        lay.addWidget(self.header)
        rule = QFrame()
        rule.setProperty("hairline", True)
        lay.addWidget(rule)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self._screens: Dict[str, Screen] = {}
        ctx.account_changed.connect(self._refresh_header)
        ctx.meta_changed.connect(self._refresh_header)
        self.guard = ClickGuard(self)
        self.guard.owned = [self]
        plugin = getattr(ctx, "plugin", None)
        menu = getattr(plugin, "menu", None) if plugin is not None else None
        if menu is not None:
            self.guard.owned.append(menu)
        self.guard.install()
        ctx.click_guard = self.guard

    def showEvent(self, event) -> None:
        """QGIS restores the dock open (panel left open last session) without going through ``open_panel``:
        route it to its entry screen, or the body stays empty."""
        super().showEvent(event)
        self.ctx.ensure_screen()
        cur = self.current_screen()
        if cur is not None:
            self.guard.stamp(cur, self.ctx.current_board)
        hook = getattr(cur, "on_panel_shown", None)
        if callable(hook):
            hook()
        for ms in (0, 120, 500):
            QTimer.singleShot(ms, self._relayout)

    def _relayout(self) -> None:
        try:
            root = self.widget()
            if root is None or not self.isVisible():
                return
            root.layout().invalidate()
            root.layout().activate()
            cur = self.current_screen()
            if cur is not None:
                if cur.layout() is not None:
                    cur.layout().invalidate()
                    cur.layout().activate()
                from ...ui.widgets import FitScrollArea

                for sa in cur.findChildren(FitScrollArea):
                    sa._fit()
                cur.updateGeometry()
        except Exception:
            pass

    def hideEvent(self, event) -> None:
        """Hidden by any route (its X, the toolbar toggle, a tab change): the screen keeps its state."""
        try:
            hook = getattr(self.current_screen(), "on_panel_hidden", None)
            if callable(hook):
                hook()
        except RuntimeError:
            pass
        super().hideEvent(event)

    def screen(self, board: str) -> Optional[Screen]:
        if board in self._screens:
            return self._screens[board]
        factory = registry.SCREENS.get(board)
        if factory is None:
            return None
        s = factory(self.ctx)
        self._screens[board] = s
        self.stack.addWidget(s)
        from ...plugin import no_menu_roles

        no_menu_roles(s)
        self.guard.watch(s)
        return s

    def show_board(self, board: str, **kw) -> Optional[Screen]:
        prev = self._screens.get(self.ctx.current_board)
        s = self.screen(board)
        if s is None:
            return None
        cur = self.stack.currentWidget()
        if cur is not None and cur is not s and isinstance(cur, Screen):
            cur.on_hide()
        self.stack.setCurrentWidget(s)
        self._refresh_header()
        s.on_show(**kw)
        if not self.isVisible():
            self.show()
        self.guard.stamp(s, board)
        return s

    def _refresh_header(self) -> None:
        self.header.refresh(self.ctx.current_board in registry.BARE_HEADER)

    def current_screen(self) -> Optional[Screen]:
        w = self.stack.currentWidget()
        return w if isinstance(w, Screen) else None

    def close_panel(self) -> None:
        """The header's close button.  The current screen's ``before_close`` may cancel it (review screens ask RV-Exit
        when lines exist)."""
        hook = getattr(self.current_screen(), "before_close", None)
        if callable(hook) and hook() is False:
            return
        cur = self.current_screen()
        keep = getattr(cur, "on_panel_hidden", None)
        if callable(keep):
            keep()
        if cur is not None:
            cur.on_hide()
        self.hide()
        self.closed_by_user.emit()
