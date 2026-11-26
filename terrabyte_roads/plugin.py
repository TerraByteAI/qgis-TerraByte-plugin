"""Plugin wiring: toolbar/menu actions and the dock."""
from __future__ import annotations

from typing import Any, Dict, Optional

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QObject, Qt, QTimer
from qgis.PyQt.QtGui import QIcon

from .core.compat import QAction
from .core.settings import PluginSettings
from .core.tasks import TaskManager

LOG_TAG = "Road Extractor"
ACCOUNT_START_DELAY_MS = 800
PANEL_NAME = "TerraByteRoadExtractorPanel"


def _safe_stale(fn) -> None:
    try:
        fn()
    except Exception:
        pass


def _delete_now(obj) -> None:
    """Destroy a Qt object we own at once.  ``deleteLater`` keeps it alive until the event loop runs; whatever is
    still alive when QgsApplication is destroyed aborts the process at interpreter exit (exit 134/139)."""
    from qgis.PyQt import sip

    try:
        sip.delete(obj)
    except (RuntimeError, TypeError):
        pass


def no_menu_role(action):
    """Every QAction the plugin creates or owns goes through here.  On macOS Qt moves an action whose role is the
    default (``TextHeuristicRole``) and whose text starts with Settings, Preferences, Options, Config, Setup, About,
    Quit or Exit into the application menu, where it replaces QGIS's own Preferences / About / Quit item.  ``NoRole``
    keeps it where the plugin put it.  Returns the action, so a creation reads ``act = no_menu_role(QAction(...))``."""
    try:
        action.setMenuRole(QAction.MenuRole.NoRole)
    except (RuntimeError, AttributeError, TypeError):
        pass
    return action


def no_menu_roles(widget) -> int:
    """``NoRole`` on every QAction under ``widget``, Qt's own included (a dock's toggle action, a line edit's actions);
    called when the panel, a screen or a flow dialog is created.  Returns how many actions it saw."""
    n = 0
    try:
        acts = widget.findChildren(QAction)
    except (RuntimeError, AttributeError, TypeError):
        return 0
    for a in acts:
        no_menu_role(a)
        n += 1
    return n


def removed_panel_was_showing(win, name: str = PANEL_NAME) -> bool:
    """Whether the dock widget ``name`` was showing when it was removed, for a previous copy of the plugin (an older
    version) that did not record it itself.  QGIS's window keeps the place of a dock widget removed from it, with
    whether it was showing, until a dock of the same name is added again.  ``saveState`` writes
    that place as the name (a QString: 32-bit big-endian byte length, then UTF-16BE) followed by a flags byte whose
    bit 0 is "visible" (Qt 5 and Qt 6).  Read before the new panel is added.  At a fresh start there is none: QGIS
    restores its window (and the panel, if it was open at the last quit) after the plugins are loaded."""
    import struct

    try:
        st = bytes(win.saveState())
    except Exception:
        return False
    key = name.encode("utf-16-be")
    i = st.find(struct.pack(">I", len(key)) + key)
    if i < 0:
        return False
    j = i + 4 + len(key)
    return j < len(st) and bool(st[j] & 1)


def log(msg: str, level=Qgis.MessageLevel.Info) -> None:
    from .core import logbuffer

    logbuffer.record(msg)
    QgsMessageLog.logMessage(str(msg), LOG_TAG, level)


class TerraByteRoadsPlugin(QObject):
    def __init__(self, iface: Any, settings: Optional[PluginSettings] = None):
        super().__init__()
        self.iface = iface
        self._settings_override = settings
        self.settings: Optional[PluginSettings] = settings
        self.tasks: Optional[TaskManager] = None
        self.ctx = None
        self.dock = None
        self.flow = None
        self.action: Optional[QAction] = None
        self._menu = "&" + "TerraByte Roads"
        self._loaded = False

    def initGui(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._incomplete_install():
            return
        from .bridge.context import BridgeContext

        self.tasks = TaskManager()
        from .core import paths

        paths.remove_old_models()
        self.settings = self.settings or PluginSettings()
        self.ctx = BridgeContext(self.iface, self.tasks, self.settings, plugin=self)

        from .ui.flow.brand import logo_icon

        icon = logo_icon()
        self.action = no_menu_role(QAction(icon, "Road Extractor", self.iface.mainWindow()))
        self.action.setCheckable(True)
        self.action.setToolTip("Road Extractor\nSeven line types from your imagery.")
        self.action.toggled.connect(self._on_action_toggled)
        self.iface.addToolBarIcon(self.action)
        self._build_menu(icon)

        from .ui.flow.controller import FlowController
        from .ui.flow.panel import RoadExtractorPanel

        from .ui.flow.settings import install_log_collector

        install_log_collector()
        self.flow = FlowController(self.iface, plugin=self, settings=self.settings, bridge_ctx=self.ctx)
        from .ui.flow.project_guard import ProjectGuard, remove_stale

        QTimer.singleShot(0, lambda: _safe_stale(remove_stale))
        self.flow.guard = ProjectGuard(self.flow)
        from .ui.flow.leave_guard import LeaveGuard

        self.flow.leave_guard = LeaveGuard(self.flow)
        reopen = self._panel_was_open()
        self.dock = RoadExtractorPanel(self.flow, self.iface.mainWindow())
        no_menu_role(self.dock.toggleViewAction())
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        no_menu_roles(self.dock)
        self.dock.visibilityChanged.connect(self._on_dock_visibility)
        self.dock.hide()
        if reopen:
            QTimer.singleShot(0, self._reopen_panel)

        self._account_timer = QTimer(self)
        self._account_timer.setSingleShot(True)
        self._account_timer.timeout.connect(lambda: self.flow.ensure_account() if self.flow is not None else None)
        self._account_timer.start(ACCOUNT_START_DELAY_MS)

    def _incomplete_install(self) -> bool:
        """True when an upgrade stopped half way and left new and old files mixed (``integrity.problems``).  Then
        nothing else runs: one message bar line and a card say what to do."""
        try:
            from . import integrity
            from .core import paths

            bad = integrity.problems(paths.PLUGIN_DIR)
        except Exception:
            return False
        if not bad:
            return False
        log(f"incomplete install, {len(bad)} files missing or different: {', '.join(bad[:8])}", Qgis.MessageLevel.Critical)
        try:
            self.iface.messageBar().pushMessage("", integrity.BAR_MESSAGE, Qgis.MessageLevel.Critical, 0)
        except Exception:
            pass
        try:
            from qgis.PyQt.QtWidgets import QDockWidget, QLabel, QVBoxLayout, QWidget

            dock = QDockWidget("Road Extractor", self.iface.mainWindow())
            dock.setObjectName("TerraByteRoadExtractorDock")
            body = QWidget()
            lay = QVBoxLayout(body)
            lb = QLabel(integrity.MESSAGE)
            lb.setWordWrap(True)
            lay.addWidget(lb)
            lay.addStretch(1)
            dock.setWidget(body)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            self.dock = dock
        except Exception:
            self.dock = None
        return True

    def unload(self) -> None:
        t = getattr(self, "_account_timer", None)
        if t is not None:
            t.stop()
        if self.tasks is not None:
            try:
                self.tasks.cancel_all()
            except Exception as e:
                log(f"error cancelling tasks: {e}", Qgis.MessageLevel.Warning)
        if self.dock is not None:
            self._remember_panel()
            try:
                if self.flow is not None:
                    self.flow.shutdown()
                self.iface.removeDockWidget(self.dock)
                _delete_now(self.dock)
            except Exception as e:
                log(f"error removing dock: {e}", Qgis.MessageLevel.Warning)
            self.dock = None
            self.flow = None
        if self.action is not None:
            try:
                self._remove_menu()
                self.iface.removeToolBarIcon(self.action)
                _delete_now(self.action)
            except Exception as e:
                log(f"error removing actions: {e}", Qgis.MessageLevel.Warning)
            self.action = None
        self._loaded = False
        try:
            from .ui import icons, theme
            from .ui.flow import brand

            icons.cleanup_icon_dir()
            brand._renderer = None
            brand._cache.clear()
            theme.unload_fonts()
        except Exception:
            pass
        self._drop_own_modules()

    def _remember_panel(self) -> None:
        """Whether the panel is open as this copy unloads (Install from ZIP over it, a reload, unticking it), for the
        copy that loads next.  Kept with this QGIS's process id: a record from an earlier session never counts (the
        plugin is unloaded at every quit too, and at a start QGIS restores the panel itself)."""
        if self.settings is None or self.dock is None:
            return
        try:
            import os

            self.settings.panel_at_unload = f"{os.getpid()}:{0 if self.dock.isHidden() else 1}"
        except Exception:
            pass

    def _panel_was_open(self) -> bool:
        """True when the copy unloaded before this one in this QGIS session left the panel open.  Its own record
        first; an older copy that recorded nothing is read from the place QGIS's window kept for its panel."""
        import os

        rec = ""
        try:
            rec = self.settings.panel_at_unload
            if rec:
                self.settings.panel_at_unload = ""
        except Exception:
            rec = ""
        pid, _, flag = str(rec).partition(":")
        if pid == str(os.getpid()) and flag in ("0", "1"):
            return flag == "1"
        try:
            return removed_panel_was_showing(self.iface.mainWindow())
        except Exception:
            return False

    def _reopen_panel(self) -> None:
        """The panel was open before the reload: open it again, on the screen a fresh start shows (the entry screen).
        A user who had closed it is not shown it."""
        try:
            if self.dock is not None and self.flow is not None and self.dock.isHidden():
                log("the panel was open before the plugin was reloaded: opened again")
                self.open_panel()
        except Exception as e:
            log(f"could not open the panel again after a reload: {e}", Qgis.MessageLevel.Warning)

    def _build_menu(self, icon) -> None:
        """Top-level "TerraByte" menu (between Processing and Help): Open panel, Settings."""
        from qgis.PyQt.QtWidgets import QMenu

        from .ui import icons

        def drawn(name):
            return icons.icon(name, 15, "#4a4755", 1.6)

        win = self.iface.mainWindow()
        self.menu = QMenu("TerraByte", win)
        self.menu.setObjectName("TerraByteRoadExtractorMenu")
        no_menu_role(self.menu.menuAction())
        self.menu_actions = {}
        entries = [("open", "Open Road Extractor panel", lambda: self.open_panel(), icon),
                   (None, None, None, None),
                   ("settings", "Settings", lambda: self.open_panel("ST-Account"), drawn("crosshair")),
                  ]
        for key, text, fn, ic in entries:
            if key is None:
                self.menu.addSeparator()
                continue
            act = no_menu_role(QAction(ic, text, self.menu) if ic is not None else QAction(text, self.menu))
            act.triggered.connect(lambda _=False, f=fn: f())
            self.menu.addAction(act)
            self.menu_actions[key] = act
        bar = win.menuBar() if hasattr(win, "menuBar") else None
        if bar is None:
            self.iface.addPluginToMenu(self._menu, self.menu_actions["open"])
            return
        help_menu = self.iface.helpMenu() if hasattr(self.iface, "helpMenu") else None
        if help_menu is not None:
            bar.insertMenu(help_menu.menuAction(), self.menu)
        else:
            bar.addMenu(self.menu)

    def _remove_menu(self) -> None:
        menu = getattr(self, "menu", None)
        if menu is None:
            return
        try:
            menu.menuAction().setVisible(False)
            win = self.iface.mainWindow()
            if hasattr(win, "menuBar"):
                win.menuBar().removeAction(menu.menuAction())
            else:
                self.iface.removePluginMenu(self._menu, self.menu_actions["open"])
            _delete_now(menu)
        finally:
            self.menu = None
            self.menu_actions = {}

    menu = None
    menu_actions: Dict[str, Any] = {}

    def _drop_own_modules(self) -> None:
        """Forget this package's modules so a reload or an in-place upgrade imports the files afresh (see
        ``terrabyte_roads.purge_modules``).  Setting TERRABYTE_ROADS_KEEP_MODULES keeps them (for a test harness that
        reuses the modules)."""
        import os

        from . import KEEP_MODULES_ENV, purge_modules

        if not os.environ.get(KEEP_MODULES_ENV):
            purge_modules(include_root=True)

    def _on_action_toggled(self, checked: bool) -> None:
        if self.dock is None:
            return
        if checked:
            self.open_panel()
        else:
            self.dock.setVisible(False)

    def open_panel(self, board: str = "") -> None:
        """Show the panel on its entry screen (EN-SignedOut when signed out, EX-Start when signed in), or on the
        screen whose key is given."""
        if self.flow is None or self.dock is None:
            return
        self.flow.ensure_account()
        if self.dock.isVisible() and not board:
            self.dock.raise_()
            return
        self.dock.show()
        if board or not self.flow.resumable():
            self.flow.go(board or self.flow.entry_board())
        self.dock.show()
        self.dock.raise_()

    def _on_dock_visibility(self, visible: bool) -> None:
        if self.action is not None and self.action.isChecked() != bool(visible):
            self.action.blockSignals(True)
            self.action.setChecked(bool(visible))
            self.action.blockSignals(False)
