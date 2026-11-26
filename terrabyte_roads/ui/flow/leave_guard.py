"""Asks the plugin's question before QGIS or the project is left while lines or a run would be lost.

The guard is held while a review holds unexported lines (``hold("review")``, from ``review.start_review`` until
``review.teardown``), while a run is in flight or its end is not handed over yet (``hold("run")``, ``run_at_stake``),
and while the run's card holds lines of charged tiles (``hold("card")``, ``card_at_stake``).  Every way of leaving
then asks the matching question first: "Leave without exporting?" (RV-Exit), "Stop extracting?"
(``extract.ask_leave_run``) or "Keep what finished?" (``extract.ask_leave_card``).  When the user chooses to leave,
QGIS's own action runs again, with its own questions.

QGIS offers one official hook, ``QgsApplicationExitBlockerInterface``: every quit asks it, but only after QGIS's own
questions (unsaved edits, scratch layers, "save the project?"), and the Project menu has no hook at all.  So, while
the guard is held:

* QGIS's Exit, New, Open and Close project actions (``iface.actionExit()`` and the others; Close is
  ``mActionCloseProject``, found by object name) are made checkable.  Qt emits an action's ``toggled`` before its
  ``triggered``; the ``toggled`` slot blocks the action's signals for that one activation, so QGIS's slot does not
  run and the question opens instead.  Leaving gives the actions back as they were and triggers the action again.
* The main window's Close event (its close button; macOS's Dock Quit and log-out close the windows) is caught by a
  filter on the main window, and the window is closed again once the user chooses to leave.
* Project > Open Recent and New from Template (``mRecentProjectsMenu``, ``mProjectFromTemplateMenu``): QGIS rebuilds
  their items, so only the menu's signals are blocked; Qt still reports the item to the parent menu (Project), and
  the menu's ``triggered(QAction*)`` is sent for it once the user chooses to leave.
* A project file dropped on the window or the Layers panel: a ``QgsCustomDropHandler`` claims .qgs / .qgz files, and
  drops them on QGIS's window again once the user chooses to leave.
* The exit blocker stays registered as the floor for any other path to ``fileExit``; it returns True at once when
  nothing is at stake and never raises.

Nothing here is an event filter on the QApplication, and nothing but the exit blocker is installed before the guard
is held.  Not covered: Project > Revert, Project > Open From (a GeoPackage or PostgreSQL project), and a project opened
from the Browser panel or the operating system ("Open with"); QGIS's own scratch-layer question still asks there.
"""
from __future__ import annotations

import functools
import weakref
from typing import Any, Callable, List, Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QEventLoop, QObject, QTimer
from qgis.PyQt.QtWidgets import QMenu

from ...core.compat import QAction

ACTIONS = (("actionExit", "quit"), ("actionNewProject", "new"), ("actionOpenProject", "open"))
CLOSE_ACTION = "mActionCloseProject"
RECENT_MENU = "mRecentProjectsMenu"
TEMPLATE_MENU = "mProjectFromTemplateMenu"
PROJECT_SUFFIXES = (".qgs", ".qgz")
HANDOVER_MS = 200
HANDOVER_TRIES = 300

try:
    from qgis.gui import QgsApplicationExitBlockerInterface
except ImportError:
    QgsApplicationExitBlockerInterface = None

try:
    from qgis.gui import QgsCustomDropHandler
except ImportError:
    QgsCustomDropHandler = None


def _named(win, cls, name: str):
    """QGIS's own object ``name`` under its window (None when this QGIS has none)."""
    try:
        return win.findChild(cls, name) if win is not None else None
    except Exception:
        return None


def _alive(o) -> bool:
    try:
        return o is not None and not sip.isdeleted(o)
    except (RuntimeError, TypeError):
        return False


if QgsCustomDropHandler is not None:
    class _DropHandler(QgsCustomDropHandler):
        """Asked by QGIS about each dropped file before it opens it; True keeps QGIS from opening that file."""

        def __init__(self, guard: "LeaveGuard"):
            super().__init__()
            self._guard = weakref.ref(guard)

        def handleFileDrop(self, file) -> bool:
            try:
                g = self._guard()
                return bool(g is not None and g.file_dropped(file))
            except Exception:
                return False
else:
    _DropHandler = None


if QgsApplicationExitBlockerInterface is not None:
    class _ExitBlocker(QgsApplicationExitBlockerInterface):
        def __init__(self, guard: "LeaveGuard"):
            super().__init__()
            import weakref

            self._guard = weakref.ref(guard)

        def allowExit(self) -> bool:
            try:
                g = self._guard()
                return True if g is None else bool(g.allow_exit())
            except Exception:
                return True
else:
    _ExitBlocker = None


class LeaveGuard(QObject):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx = ctx
        self._armed: List[tuple] = []
        self._win = None
        self._recent = None
        self._recent_menu = None
        self._menus: List[tuple] = []
        self._holds: set = set()
        self._drop = None
        self._drops: List[str] = []
        self._resetting = False
        self._asking = False
        self.asked: List[str] = []
        self._blocker = None
        reg = getattr(ctx.iface, "registerApplicationExitBlocker", None)
        if _ExitBlocker is not None and callable(reg):
            try:
                self._blocker = _ExitBlocker(self)
                reg(self._blocker)
            except Exception:
                self._blocker = None

    def dispose(self) -> None:
        self.disarm()
        b, self._blocker = self._blocker, None
        unreg = getattr(self.ctx.iface, "unregisterApplicationExitBlocker", None)
        if b is not None and callable(unreg):
            try:
                unreg(b)
            except Exception:
                pass

    def hold(self, why: str) -> None:
        """Arm for ``why`` ("review": a review holds lines; "run": a run is at stake; "card": the run's card holds
        lines)."""
        self._holds.add(why)
        self.arm()

    def release(self, why: str) -> None:
        """``why`` no longer holds the guard; QGIS gets its actions back when nothing does."""
        self._holds.discard(why)
        if not self._holds:
            self.disarm()

    def run_at_stake(self) -> bool:
        """A run is in flight (checked, uploaded, started, extracted, or stopping), or its end is not handed over
        yet: the Stop dialog shows or waits for its charge, or its lines are on their way to the review."""
        run = getattr(self.ctx, "run", None)
        try:
            if run is None or getattr(run, "_dead", False):
                return False
            if run.running or getattr(run, "checking", False) or self._end_on_its_way(run):
                return True
            s = self._running_screen()
            return bool(s is not None and s.holds_run_end(run))
        except RuntimeError:
            return False

    @staticmethod
    def _end_on_its_way(run) -> bool:
        """The run ended and its result is being fetched for the review."""
        return run is not None and not run.running and getattr(run, "review_fetch", None) is not None

    def _running_screen(self):
        """The Running screen if the panel made one (never made here)."""
        panel = getattr(self.ctx, "panel", None)
        try:
            return getattr(panel, "_screens", {}).get("EX-Running") if panel is not None else None
        except (AttributeError, RuntimeError):
            return None

    def refresh_run(self) -> None:
        """Hold the guard for the run exactly while ``run_at_stake`` (the flow calls this when the run or its Stop
        dialog moves on)."""
        if getattr(self.ctx, "shut_down", False):
            return
        if self.run_at_stake():
            if "run" not in self._holds:
                self.hold("run")
        elif "run" in self._holds:
            self.release("run")

    def card_at_stake(self) -> bool:
        """The run's card (EX-Failed after a lost connection or a stop) is up and the plugin holds lines of tiles
        TerraByte charged.  The service's copy was deleted at the run's end, so these are the only ones."""
        ctx = self.ctx
        run = getattr(ctx, "run", None)
        if getattr(ctx, "current_board", "") != "EX-Failed" or run is None or getattr(run, "_dead", False):
            return False
        panel = getattr(ctx, "panel", None)
        try:
            s = getattr(panel, "_screens", {}).get("EX-Failed") if panel is not None else None
            return (s is not None and getattr(s, "shown_kind", "") in ("connection", "stopped")
                    and int(getattr(run, "tiles_held", 0) or 0) > 0)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    def refresh_card(self) -> None:
        """Hold the guard for the run's card exactly while ``card_at_stake`` (its screen calls this on show and hide)."""
        if getattr(self.ctx, "shut_down", False):
            return
        if self.card_at_stake():
            if "card" not in self._holds:
                self.hold("card")
        elif "card" in self._holds:
            self.release("card")

    def at_stake(self) -> bool:
        return self.lines_at_stake() or self.run_at_stake() or self.card_at_stake()

    def lines_at_stake(self) -> bool:
        s = getattr(self.ctx, "review", None)
        try:
            return s is not None and not s.closed and bool(s.has_lines())
        except RuntimeError:
            return False

    @property
    def armed(self) -> bool:
        return bool(self._armed or self._menus or self._drop is not None)

    def arm(self) -> None:
        if self.armed:
            return
        iface = self.ctx.iface
        for name, kind in ACTIONS:
            getter = getattr(iface, name, None)
            try:
                a = getter() if callable(getter) else None
            except Exception:
                a = None
            if a is not None:
                self._arm_action(a, kind)
        try:
            win = iface.mainWindow()
        except Exception:
            win = None
        close = _named(win, QAction, CLOSE_ACTION)
        if close is not None and not any(e[0] is close for e in self._armed):
            self._arm_action(close, "close_project")
        self._arm_recent(win)
        self._arm_menu(win, TEMPLATE_MENU, "new_template")
        self._arm_drop()
        try:
            self._win = win
            self._win.installEventFilter(self)
        except Exception:
            self._win = None

    def _arm_recent(self, win) -> None:
        if self._arm_menu(win, RECENT_MENU, "open_recent"):
            self._recent = self._menus[-1][:3]
            self._recent_menu = self._recent[0]

    def _arm_menu(self, win, name: str, kind: str) -> bool:
        """A submenu of Project whose items QGIS opens from the MENU's ``triggered``: its signals are blocked while armed,
        and its items are heard from the parent menu (Qt tells it about its submenus' items)."""
        menu = _named(win, QMenu, name)
        try:
            parent = menu.parentWidget() if menu is not None else None
        except RuntimeError:
            parent = None
        if not isinstance(parent, QMenu) or any(m[0] is menu for m in self._menus):
            return False
        try:
            was = menu.signalsBlocked()
            if not any(m[2] is parent for m in self._menus):
                parent.triggered.connect(self._recent_triggered)
            menu.blockSignals(True)
            self._menus.append((menu, was, parent, kind))
            return True
        except Exception:
            if not any(m[2] is parent for m in self._menus):
                try:
                    parent.triggered.disconnect(self._recent_triggered)
                except (RuntimeError, TypeError):
                    pass
            return False

    def _arm_drop(self) -> None:
        reg = getattr(self.ctx.iface, "registerCustomDropHandler", None)
        if _DropHandler is None or not callable(reg) or self._drop is not None:
            return
        try:
            h = _DropHandler(self)
            reg(h)
            self._drop = h
        except Exception:
            self._drop = None

    def _arm_action(self, a, kind: str) -> None:
        try:
            was = a.isCheckable()
            a.setCheckable(True)
            slot = functools.partial(self._toggled, a, kind)
            a.toggled.connect(slot)
            self._armed.append((a, was, slot, kind))
        except Exception:
            pass

    def disarm(self) -> None:
        """QGIS's actions exactly as they were, no filter on the window."""
        armed, self._armed = self._armed, []
        for a, was, slot, _kind in armed:
            self._restore(a, was, slot)
        self._recent = None
        menus, self._menus = self._menus, []
        parents = []
        for menu, was, parent, _kind in menus:
            if not any(p is parent for p in parents):
                parents.append(parent)
                try:
                    parent.triggered.disconnect(self._recent_triggered)
                except (RuntimeError, TypeError):
                    pass
            try:
                menu.blockSignals(was)
            except RuntimeError:
                pass
        h, self._drop = self._drop, None
        self._drops = []
        unreg = getattr(self.ctx.iface, "unregisterCustomDropHandler", None)
        if h is not None and callable(unreg):
            try:
                unreg(h)
            except Exception:
                pass
        win, self._win = self._win, None
        if win is not None:
            try:
                win.removeEventFilter(self)
            except RuntimeError:
                pass

    def _restore(self, a, was: bool, slot) -> None:
        try:
            a.toggled.disconnect(slot)
        except (RuntimeError, TypeError):
            pass
        try:
            a.blockSignals(False)
            self._resetting = True
            a.setChecked(False)
            a.setCheckable(was)
        except RuntimeError:
            pass
        finally:
            self._resetting = False

    def _toggled(self, a, kind: str, _checked: bool = False) -> None:
        if self._resetting:
            return
        try:
            a.blockSignals(True)
        finally:
            QTimer.singleShot(0, functools.partial(self._held, a, kind))

    def _held(self, a, kind: str) -> None:
        try:
            a.blockSignals(False)
            self._resetting = True
            try:
                a.setChecked(False)
            finally:
                self._resetting = False
            if not self.at_stake():
                self._heal()
                self._pass(a)
                return
            self._ask(kind, functools.partial(self._pass, a))
        except Exception:
            try:
                a.blockSignals(False)
            except RuntimeError:
                pass
            self._pass(a)

    def _pass(self, a) -> None:
        """Let QGIS's own action run: it is given back first (as it was), and taken again if a review is still open."""
        entry = next((e for e in self._armed if e[0] is a), None)
        if entry is not None:
            self._armed.remove(entry)
            self._restore(entry[0], entry[1], entry[2])
        try:
            a.trigger()
        finally:
            if self._holds and entry is not None and not any(e[0] is a for e in self._armed):
                self._arm_action(a, entry[3])

    def _recent_triggered(self, action) -> None:
        """An item of a menu under Project was chosen (Qt tells the parent menu about its submenus' items)."""
        try:
            if not self._menus or action is None or self._resetting:
                return
            owner = sip.unwrapinstance(action.parent())
            held = next((m for m in self._menus if sip.unwrapinstance(m[0]) == owner), None)
            if held is None:
                return
        except Exception:
            return
        QTimer.singleShot(0, functools.partial(self._recent_held, action, held[0], held[3]))

    def _recent_held(self, action, menu=None, kind: str = "open_recent") -> None:
        if getattr(self.ctx, "shut_down", False):
            return
        menu = menu if menu is not None else self._recent_menu
        try:
            if not self.at_stake() or (kind == "open_recent" and action.data() is None):
                self._heal()
                self._give_recent(action, menu)
                return
            self._ask(kind, functools.partial(self._give_recent, action, menu))
        except Exception:
            self._give_recent(action, menu)

    def _give_recent(self, action, menu=None) -> None:
        """QGIS's own slot for that item: the menu's ``triggered`` (QgisApp::openProject(QAction*) for Open Recent,
        fileNewFromTemplateAction for New from Template), sent with the menu's signals let through for that one call."""
        menu = menu if menu is not None else self._recent_menu
        if not (_alive(menu) and _alive(action)):
            return
        try:
            held = lambda: any(m[0] is menu for m in self._menus)
            armed_before, blocked = held(), menu.signalsBlocked()
            menu.blockSignals(False)
            try:
                menu.triggered.emit(action)
            finally:
                if held():
                    menu.blockSignals(True)
                elif not armed_before:
                    menu.blockSignals(blocked)
        except RuntimeError:
            pass

    def file_dropped(self, path) -> bool:
        """QGIS asks before it opens a dropped file: a project file while lines are at stake is held for the question."""
        p = str(path or "")
        if not p.lower().endswith(PROJECT_SUFFIXES) or not self.at_stake():
            return False
        self._drops.append(p)
        if len(self._drops) == 1:
            QTimer.singleShot(0, self._drop_held)
        return True

    def _drop_held(self) -> None:
        files, self._drops = list(self._drops), []
        if not files or getattr(self.ctx, "shut_down", False):
            return
        if not self.at_stake():
            self._redrop(files)
            return
        self._ask("drop", functools.partial(self._redrop, files))

    def _redrop(self, files: List[str]) -> None:
        """QGIS's own drop path for those files (QgisApp::dropEvent, then openFile, which asks QGIS's own questions
        about the project first): they are dropped on QGIS's window again, as a drag does it (a drop reaches a window
        only after it took the drag's enter)."""
        from qgis.PyQt.QtCore import QCoreApplication, QMimeData, QPointF, Qt, QUrl
        from qgis.PyQt.QtGui import QDragEnterEvent, QDropEvent

        try:
            win = self.ctx.iface.mainWindow()
            md = QMimeData()
            md.setUrls([QUrl.fromLocalFile(f) for f in files])
            pos = win.rect().center()
            how = (Qt.DropAction.CopyAction, md, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
            enter = QDragEnterEvent(pos, *how)
            QCoreApplication.sendEvent(win, enter)
            if not enter.isAccepted():
                from ...plugin import log

                log(f"QGIS's window did not take the dropped project again: {', '.join(files)}")
                return
            QCoreApplication.sendEvent(win, QDropEvent(QPointF(pos), *how))
        except Exception:
            pass

    def eventFilter(self, o, ev):
        try:
            if ev.type() != QEvent.Type.Close or o is not self._win or not self.at_stake():
                return False
            win = self._win
            ev.ignore()
            self._ask("close", win.close)
            return True
        except Exception:
            return False

    def _heal(self) -> None:
        """A run that is no longer in flight, or a card no longer up, still holds the guard: release."""
        if "run" in self._holds and not self.run_at_stake():
            QTimer.singleShot(0, lambda: self.release("run") if not self.run_at_stake() else None)
        if "card" in self._holds and not self.card_at_stake():
            QTimer.singleShot(0, lambda: self.release("card") if not self.card_at_stake() else None)

    def _ask(self, kind: str, proceed: Optional[Callable[[], Any]]) -> Any:
        """Open RV-Exit (a review with lines), the run's question (a run at stake) or the card's question
        (``card_at_stake``); ``proceed`` runs once the user chooses to leave (the review or the run is gone by then)."""
        ctx = self.ctx
        self.asked.append(kind)
        ctx.after_leave = proceed
        panel = getattr(ctx, "panel", None)
        try:
            if panel is not None and not panel.isVisible():
                panel.show()
        except RuntimeError:
            pass
        if not self.lines_at_stake() and self.run_at_stake():
            from .extract import ask_leave_run

            dlg = ask_leave_run(ctx, kind)
            if dlg is None and self._end_on_its_way(getattr(ctx, "run", None)):
                ctx.after_leave = None
                self._ask_when_handed_over(kind, proceed)
                return None
            if dlg is None:
                ctx.after_leave = None
                if proceed is not None:
                    proceed()
            return dlg
        if not self.lines_at_stake() and self.card_at_stake():
            from .extract import ask_leave_card

            dlg = ask_leave_card(ctx, kind)
            if dlg is None:
                ctx.after_leave = None
                if proceed is not None:
                    proceed()
            return dlg
        dlg = ctx.go("RV-Exit")
        if dlg is None:
            ctx.after_leave = None
            if proceed is not None:
                proceed()
        return dlg

    def _ask_when_handed_over(self, kind: str, proceed: Optional[Callable[[], Any]], tries: int = 0) -> None:
        """A leave while the run's lines are on their way to the review waits for the review and asks its question
        then; if the lines do not come (a card says why), the leave is dropped (the user leaves from there)."""
        if getattr(self.ctx, "shut_down", False):
            return
        if self.lines_at_stake():
            self._ask(kind, proceed)
        elif self._end_on_its_way(getattr(self.ctx, "run", None)) and tries < HANDOVER_TRIES:
            QTimer.singleShot(HANDOVER_MS, lambda: self._ask_when_handed_over(kind, proceed, tries + 1))

    def allow_exit(self) -> bool:
        """The exit blocker: True at once with nothing at stake; otherwise the question, and True only after "Discard
        and exit" (the review is gone) or "Leave" (the run is stopped).  The answer follows what the user chose, not
        what is at stake afterwards (after Stay or Keep what finished nothing may be at stake for a moment)."""
        if not self.at_stake():
            return True
        if self._asking:
            return False
        self._asking = True
        try:
            self.ctx.leave_chosen = None
            dlg = self._ask("quit", None)
            if dlg is not None and dlg.isVisible():
                loop = QEventLoop()
                dlg.finished.connect(lambda *_a: loop.quit())
                loop.exec()
            chosen = getattr(self.ctx, "leave_chosen", None)
            return bool(chosen) if chosen is not None else not self.at_stake()
        finally:
            self._asking = False
