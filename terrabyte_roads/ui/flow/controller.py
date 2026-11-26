"""FlowController: shared state, auth, navigation and services for every flow screen.

Flow modules receive the controller as ``ctx``.  Public surface:

    ctx.iface, ctx.plugin, ctx.settings, ctx.client, ctx.tokens, ctx.bridge_ctx
    ctx.meta (dict, from /v1/meta; {} until loaded), ctx.account (dict | None, from /v1/me)
    ctx.go(screen_id, **kw)         navigate (dock screen, dialog, or alias)
    ctx.panel                       the dock widget (set by the panel)
    ctx.current_board               id of the screen in the dock
    ctx.dialogs                     {screen_id: QDialog} of dialogs currently open
    ctx.tiles_left()                int | None;  ctx.tiles_chip_text()
    ctx.set_session(token, account) / ctx.sign_out_local()
    ctx.refresh_account(cb=None)    re-reads /v1/me, emits account_changed
    ctx.plan_name() / ctx.support_email() / ctx.retention_period()  (None when the service gave none)
    ctx.open_url(url) -> bool       False when no browser could be opened
    ctx.bring_qgis_forward()        ask the window system for the front (sign-in finished in the browser)
    ctx.imagery, ctx.area, ctx.run, ctx.review    flow state objects (None until set)
    ctx.start_review(result, summary)             implemented by the review module (hook)
Signals: account_changed, meta_changed, navigated(str screen_id), gate_changed(bool)
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, Optional

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QApplication

from ...cloud.client import ApiError, CloudClient
from ...cloud.session import TokenStore
from ...core.version import plugin_version
from . import registry

LOG_TAG = "Road Extractor"
_BRACKETED = re.compile(r"\[[^\]]*\]")
ACCOUNT_RETRY_MS = (5000, 10000, 20000, 40000, 60000)
RECOVER_MIN_S = 5.0
BALANCE_SETTLE_MS = (2000, 8000, 20000, 45000, 90000, 150000)
BALANCE_FRESH_S = 10.0
_SAFE_LAUNCHERS = ("WindowsDefault", "MacOSXOSAScript", "MacOSX")


def real_text(value: Any) -> Optional[str]:
    """A string the service sent, or None when it is empty or still a bracket placeholder such as
    ``[Plan]``: the plugin hides a row rather than print one."""
    if value is None or isinstance(value, bool):
        return None
    v = str(value).strip()
    if not v or _BRACKETED.search(v):
        return None
    return v


def log(msg: str, level=Qgis.MessageLevel.Info) -> None:
    from ...core import logbuffer

    logbuffer.record(msg)
    QgsMessageLog.logMessage(str(msg), LOG_TAG, level)


def launch_url(url: str) -> bool:
    """Open a web address in the browser: True when a browser was started (as far as the system tells), False when
    none could be.  Qt's handler first.  When it fails or raises (QGIS 3.44 on a Linux desktop without xdg-open: Qt
    logs "Unable to detect a web browser to launch"), Python's webbrowser where that is safe (``_python_browser``).
    The address is not logged: the sign-in address carries the sign-in's id."""
    try:
        if QDesktopServices.openUrl(QUrl(url)):
            return True
        log("The system could not open a browser; trying Python's webbrowser.", Qgis.MessageLevel.Warning)
    except Exception as e:
        log(f"Opening the browser failed ({type(e).__name__}); trying Python's webbrowser.", Qgis.MessageLevel.Warning)
    return _python_browser(url)


def _python_browser(url: str) -> bool:
    """The second try: Python's ``webbrowser.get()`` (the platform's first choice), used only when it is a launcher that
    hands the address over and returns at once: on Linux xdg-open, gio, gvfs-open or x-www-browser (BackgroundBrowser),
    on Windows and macOS the system's own handler.  Never a console browser (lynx, w3m, links) or a command that waits
    for the browser to exit (GenericBrowser), nor a browser command that may wait 5 s for a running browser to answer
    (UnixBrowser: firefox, chromium): each would freeze QGIS.  True means the launcher started; whether it then found
    a browser is not known."""
    try:
        import webbrowser

        b = webbrowser.get()
    except Exception:
        log("Python's webbrowser found no browser either.", Qgis.MessageLevel.Warning)
        return False
    background = getattr(webbrowser, "BackgroundBrowser", None)
    if not ((background is not None and isinstance(b, background)) or type(b).__name__ in _SAFE_LAUNCHERS):
        log(f"Not using Python's {type(b).__name__} launcher: it could freeze QGIS.", Qgis.MessageLevel.Warning)
        return False
    try:
        return bool(b.open(url, new=2))
    except Exception as e:
        log(f"Python's webbrowser could not open a browser ({type(e).__name__}).", Qgis.MessageLevel.Warning)
        return False


class FlowController(QObject):
    account_changed = pyqtSignal()
    meta_changed = pyqtSignal()
    navigated = pyqtSignal(str)
    gate_changed = pyqtSignal(bool)

    def __init__(self, iface: Any, plugin: Any = None, settings: Any = None, bridge_ctx: Any = None,
                 client: Optional[CloudClient] = None):
        super().__init__()
        self.iface = iface
        self.plugin = plugin
        self.settings = settings
        self.bridge_ctx = bridge_ctx
        self.tokens = TokenStore(settings)
        self.client = client or CloudClient(token_getter=self.tokens.get, plugin_version=plugin_version(),
                                            settings=settings)
        self.client.session_expired.connect(self._on_session_expired)
        self.client.update_required.connect(self._on_update_required)
        reached = getattr(self.client, "reached", None)
        if reached is not None:
            reached.connect(self._on_service_reached)
        self.meta: Dict[str, Any] = {}
        self.account: Optional[Dict[str, Any]] = None
        self.panel = None
        self.after_leave = None
        self.leave_chosen = None
        self.guard = None
        self.leave_guard = None
        self.current_board = ""
        self.dialogs: Dict[str, Any] = {}
        self.update_gate: Optional[Dict[str, Any]] = None
        self.update_later = False
        self.account_loading = False
        self.account_failed = False
        self.meta_failed = False
        self.click_guard = None
        self.shut_down = False
        self._retry_n = 0
        self._last_recover = 0.0
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._retry_account)
        self.account_at = 0.0
        self._settle_left: list = []
        self._settle_t0 = 0.0
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._settle_read)
        self.imagery = None
        self.area = None
        self.run = None
        self.review = None
        self.extras: Dict[str, Any] = {}

    def load_meta(self, cb: Optional[Callable[[], None]] = None) -> None:
        def ok(res):
            self.meta_failed = False
            self.meta = res or {}
            self.meta_changed.emit()
            self._check_version()
            if cb:
                cb()

        def fail(err):
            self.meta_failed = True
            log(f"/v1/meta failed: {err.message}", Qgis.MessageLevel.Warning)
            self._schedule_retry()
            if cb:
                cb()
        self.client.meta(ok=ok, err=fail)

    def _check_version(self) -> None:
        mn = self.meta.get("min_plugin_version")
        if mn and _vkey(plugin_version()) < _vkey(mn):
            self._on_update_required({"min_version": mn, "latest_version": self.meta.get("latest_plugin_version")})

    def refresh_account(self, cb: Optional[Callable[[Optional[ApiError]], None]] = None) -> None:
        self.account_loading = True

        def ok(res):
            self.account_loading = False
            self.account_failed = False
            self.account = res
            self.account_at = time.monotonic()
            if not self.meta_failed:
                self._retry_n = 0
                self._retry.stop()
            self.account_changed.emit()
            if cb:
                cb(None)

        def fail(err):
            self.account_loading = False
            self.account_failed = True
            self._schedule_retry()
            self.account_changed.emit()
            if cb:
                cb(err)
        self.client.me(ok=ok, err=fail)

    def ensure_account(self) -> None:
        """Signed in with nothing loaded yet (QGIS just started, or the plugin was installed over a loaded
        copy): read /v1/me and /v1/meta now, so the tile balance shows without waiting for a run."""
        if not self.signed_in:
            return
        if self.account is None and not self.account_loading:
            self.refresh_account()
        if not self.meta:
            self.load_meta()

    def _unknown(self) -> bool:
        """Signed in, and the account or the service's metadata is not known (or the last read of it failed)."""
        return not self.shut_down and self.signed_in and self.update_gate is None and (
            self.account is None or self.account_failed or not self.meta)

    def _schedule_retry(self) -> None:
        if not self._unknown() or self._retry.isActive():
            return
        delays = ACCOUNT_RETRY_MS
        self._retry.start(int(delays[min(self._retry_n, len(delays) - 1)]))
        self._retry_n += 1

    def _retry_account(self) -> None:
        if not self._unknown():
            self._retry_n = 0
            return
        self._recover()

    def _recover(self) -> None:
        self._last_recover = time.monotonic()
        if (self.account is None or self.account_failed) and not self.account_loading:
            self.refresh_account()
        if not self.meta:
            self.load_meta()

    def _on_service_reached(self, path: str = "") -> None:
        """Another call got an answer from the service: if the account read had failed, read it again now."""
        if str(path).split("?")[0] in ("/v1/me", "/v1/meta") or not self._unknown():
            return
        if time.monotonic() - self._last_recover < RECOVER_MIN_S:
            return
        self._retry.stop()
        self._recover()

    @property
    def signed_in(self) -> bool:
        return bool(self.tokens.get())

    def set_session(self, token: str, account: Optional[Dict[str, Any]]) -> None:
        self.tokens.set(token)
        self.account = account
        if account is not None:
            self.account_at = time.monotonic()
        self.account_changed.emit()

    def sign_out_local(self) -> None:
        self.tokens.clear()
        self.account = None
        self._retry.stop()
        self._retry_n = 0
        self.account_changed.emit()

    def _on_session_expired(self) -> None:
        if self.signed_in:
            self.sign_out_local()
            self.go("EN-SignedOut")

    def _on_update_required(self, body: Dict[str, Any]) -> None:
        self.update_gate = dict(body or {})
        self.gate_changed.emit(True)
        self.go("EN-Update")

    def tiles_left(self) -> Optional[int]:
        try:
            return int(self.account["tiles"]["left"])
        except (TypeError, KeyError, ValueError):
            return None

    def set_tiles_left(self, n: int) -> None:
        """The balance as the service just gave it in another answer (a run created, a run refused)."""
        if self.account is not None:
            self.account.setdefault("tiles", {})["left"] = int(n)
            self.account_at = time.monotonic()
            self.account_changed.emit()

    def balance_fresh(self) -> bool:
        """The balance came from the service within BALANCE_FRESH_S and no newer read is under way."""
        return (self.tiles_left() is not None and not self.account_loading and not self.account_failed
                and time.monotonic() - self.account_at <= BALANCE_FRESH_S)

    def read_balance(self, cb: Optional[Callable[[], None]] = None) -> None:
        """Read the balance from the service now and call ``cb`` once it is known (or the read failed);
        ``cb`` is called at once when the balance is fresh."""
        if self.balance_fresh() or not self.signed_in:
            if cb:
                cb()
            return
        self.refresh_account(cb=(lambda _err: cb()) if cb else None)

    def after_run_left(self) -> None:
        """A run ended, was stopped or was abandoned.  The service settles it a little later (the tiles in
        flight finish, unused reserved tiles come back): read the balance now and again over the next minutes, so every
        screen and Settings show the service's figure, not the one from before the run ended."""
        if self.shut_down or not self.signed_in:
            return
        self.account_at = 0.0
        self.refresh_account()
        self._settle_left = list(BALANCE_SETTLE_MS)
        self._settle_t0 = time.monotonic()
        self._next_settle()

    def _next_settle(self) -> None:
        self._settle.stop()
        if not self._settle_left or self.shut_down:
            return
        due_ms = self._settle_left.pop(0) - (time.monotonic() - self._settle_t0) * 1000.0
        self._settle.start(max(0, int(due_ms)))

    def _settle_read(self) -> None:
        if self.shut_down or not self.signed_in:
            return
        if not self.account_loading:
            self.refresh_account()
        self._next_settle()

    def plan_name(self) -> Optional[str]:
        return real_text(((self.account or {}).get("plan") or {}).get("name"))

    def support_email(self) -> Optional[str]:
        return real_text(self.meta.get("support_email"))

    def retention_period(self) -> Optional[str]:
        return real_text(self.meta.get("retention_period"))

    def url(self, key: str) -> str:
        """terms_url, privacy_url, what_changed_url, how_it_works_url, falling back to terrabyte.ai."""
        return self.meta.get(key) or "https://terrabyte.ai"

    def real_url(self, key: str) -> Optional[str]:
        """A page URL the service sent for ``key``, or None when it sent none or only a bare home page
        (no link is better than a wrong one)."""
        v = real_text(self.meta.get(key))
        if not v:
            return None
        bare = v.lower().rstrip("/")
        for pre in ("https://", "http://"):
            if bare.startswith(pre):
                bare = bare[len(pre):]
        if bare.startswith("www."):
            bare = bare[4:]
        if bare in ("terrabyte.ai", "plugin.terrabyte.ai") or "/" not in bare:
            return None
        return v

    def open_url(self, url: str) -> bool:
        """A mail link in the mail app (``open_mail``, which always says something), anything else in the browser
        (``launch_url``).  False when no browser could be opened, so the sign-in screen can say so."""
        if str(url).lower().startswith("mailto:"):
            self.open_mail(url)
            return True
        return launch_url(url)

    def bring_qgis_forward(self) -> None:
        """Sign-in finished in the browser, which has the front: ask the window system to bring QGIS's main window
        forward, and to flash its taskbar entry or bounce its Dock icon where it will not hand over the focus
        (focus-stealing prevention, Wayland, newer macOS).  Each call is a request the system may turn down.  Nothing
        is installed or kept (no event filter, no timer); a failure is only logged."""
        if self.shut_down:
            return
        try:
            win = self.iface.mainWindow()
        except Exception:
            win = None
        if win is None:
            return
        for what, step in (("raise", win.raise_), ("activate", win.activateWindow),
                           ("alert", lambda: QApplication.alert(win))):
            try:
                step()
            except Exception as e:
                log(f"Bringing QGIS forward: {what} failed ({type(e).__name__}: {e}).", Qgis.MessageLevel.Warning)

    def open_mail(self, url: str) -> None:
        """Open the mail app, or, when this PC has none, copy the address and say so.  The operating system's
        own "you need a new app" prompt is never shown."""
        from .contact import copy_address, mail_handler_exists

        if mail_handler_exists():
            try:
                if QDesktopServices.openUrl(QUrl(url)):
                    return
            except Exception:
                pass
        addr = copy_address(url)
        try:
            self.iface.messageBar().pushMessage("Road Extractor", f"Email {addr}. The address is copied to the clipboard.",
                                                Qgis.MessageLevel.Info, 8)
        except Exception:
            pass

    def go(self, board: str, **kw) -> Any:
        """Show a screen by id, resolving aliases first.  Dock screens replace the panel page; dialogs open over it.
        Signed out, only the sign-in screens can be shown; while an update is required, only EN-Update."""
        registry.load_lanes()
        if board in registry.ALIASES:
            board = registry.ALIASES[board](self, **kw) or board
        if not self.signed_in and board not in ("EN-SignedOut", "EN-SignInWait", "EN-Update"):
            if self.current_board == "EN-SignInWait" and self.panel is not None:
                self._close_dialogs()
                self.panel.show()
                return self.panel.screen("EN-SignInWait")
            board, kw = "EN-SignedOut", {}
        if self.update_gate is not None and board not in ("EN-Update",):
            board = "EN-Update"
        if board in registry.DIALOGS:
            return self._open_dialog(board, **kw)
        if board in registry.SCREENS and self.panel is not None:
            self._close_dialogs(except_for=None if kw.pop("_keep_dialogs", False) else None)
            self.current_board = board
            self.panel.show_board(board, **kw)
            self.navigated.emit(board)
            return self.panel.screen(board)
        log(f"go({board!r}): no such board is registered", Qgis.MessageLevel.Warning)
        return None

    def _open_dialog(self, board: str, **kw):
        old = self.dialogs.pop(board, None)
        if old is not None:
            try:
                old.close()
                old.deleteLater()
            except RuntimeError:
                pass
        dlg = registry.DIALOGS[board](self, **kw)
        if dlg is None:
            return None
        self.dialogs[board] = dlg
        dlg.finished.connect(lambda _r, b=board, d=dlg: self._dialog_closed(b, d))
        from ...plugin import no_menu_roles

        no_menu_roles(dlg)
        dlg.open()
        if self.click_guard is not None:
            self.click_guard.stamp(dlg, board)
        self.navigated.emit(board)
        return dlg

    def _dialog_closed(self, board: str, dlg) -> None:
        if self.dialogs.get(board) is dlg:
            self.dialogs.pop(board, None)
        try:
            dlg.deleteLater()
        except RuntimeError:
            pass

    def _close_dialogs(self, except_for=None) -> None:
        for b, d in list(self.dialogs.items()):
            try:
                d.done(0)
            except RuntimeError:
                pass
        self.dialogs.clear()

    def close_dialog(self, board: str, result: int = 0) -> None:
        d = self.dialogs.pop(board, None)
        if d is not None:
            try:
                d.done(result)
            except RuntimeError:
                pass

    def entry_board(self) -> str:
        """Where the panel opens: EN-Update while an update is required, else EN-SignedOut or EX-Start."""
        if self.update_gate is not None:
            return "EN-Update"
        return "EX-Start" if self.signed_in else "EN-SignedOut"

    RESUME_BOARDS = ("EX-DrawArea", "EX-Setup", "EX-OverBudget", "EX-Running", "EX-Failed", "RV-Recap")
    RESUME_REVIEW_BOARDS = ("RV-Keep", "RV-Export")

    def resumable(self) -> bool:
        """True when reopening the panel should show the screen it had, not the entry screen."""
        if not self.signed_in or self.update_gate is not None:
            return False
        b = self.current_board
        if b in self.RESUME_BOARDS:
            return True
        return b in self.RESUME_REVIEW_BOARDS and self.review is not None

    def ensure_screen(self) -> None:
        """The dock became visible with no screen yet (QGIS restored it on startup): show the entry screen."""
        if self.current_board or self.panel is None:
            return
        self.ensure_account()
        self.go(self.entry_board())

    def start_review(self, result: Dict[str, Any], summary: Optional[Dict[str, Any]] = None) -> None:
        """Open RV-Keep on a run result (implemented by ``review.start_review(ctx, result, summary)``)."""
        from . import review

        review.start_review(self, result, summary)

    def shutdown(self) -> None:
        """Plugin unload: close dialogs and take everything we put on the canvas or in the project back out.

        A map tool, overlay or review layer still alive when QgsApplication is destroyed aborts the process at
        interpreter exit (exit 134/139), so nothing may outlive the plugin."""
        self.shut_down = True
        self._close_dialogs()
        self._retry.stop()
        self._settle.stop()
        self._settle_left = []
        if self.click_guard is not None:
            self.click_guard.dispose()
            self.click_guard = None
        if self.guard is not None:
            self.guard.dispose()
            self.guard = None
        if self.leave_guard is not None:
            self.leave_guard.dispose()
            self.leave_guard = None
        for step in (self._shutdown_draw, self._shutdown_review, self._shutdown_overlays, self._shutdown_run, self._shutdown_client):
            try:
                step()
            except Exception as e:
                log(f"shutdown: {type(e).__name__}: {e}", Qgis.MessageLevel.Warning)

    def _shutdown_draw(self) -> None:
        """A rectangle still on the map from the Draw screen (tool and preview) goes with the plugin."""
        p = self.panel
        sc = p.screen("EX-DrawArea") if p is not None and "EX-DrawArea" in getattr(p, "_screens", {}) else None
        if sc is not None and getattr(sc, "tool", None) is not None:
            sc._release_tool(keep=False)

    def _shutdown_review(self) -> None:
        rv, self.review = self.review, None
        if rv is not None:
            rv.close()

    def _shutdown_run(self) -> None:
        run, self.run = self.run, None
        if run is not None:
            try:
                if run.running:
                    run.stop_quietly(wait_ms=300)
            except Exception:
                pass
            run.dispose()
        self.imagery = self.area = None

    def _shutdown_client(self) -> None:
        """Abort requests still in flight: a reply alive at exit would outlive QgsApplication."""
        for call in list(getattr(self.client, "_live", ())):
            call.cancel()

    def _shutdown_overlays(self) -> None:
        ov = getattr(self, "_review_overlays", None)
        self._review_overlays = None
        if ov is not None:
            ov.delete()
        run_ov = self.extras.pop("overlay", None)
        if run_ov is not None:
            run_ov.clear()


def _vkey(v: str):
    out = []
    for p in str(v).split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)
