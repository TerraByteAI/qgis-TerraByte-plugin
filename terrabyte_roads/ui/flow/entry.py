"""Entry screens: EN-SignedOut, EN-SignInWait, EN-BeforeStart, EN-Update (+ the Update card)."""
from __future__ import annotations

from typing import Any, Callable, Optional

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import QDialog, QFrame, QHBoxLayout, QLineEdit, QWidget

from ...cloud import terms
from ...core.version import plugin_version
from .. import icons, theme
from ..widgets import (card_dialog, IconTile, PillArrowButton, Screen, SectionHeading, Spinner, Swatch, button,
                       card, class_swatch, hbox, icon_button, label, link_color, track, vbox)
from . import registry
from .brand import RingIcon
from .controller import _vkey, log

HOW_IT_WORKS_URL = "https://terrabyte.ai"
CODE_DELAY_MS = 60_000
INVALID_CODE_COPY = "That code didn't work. Check it and try again."
CODE_PLACEHOLDER = "ABCD-1234"
OPENED_COPY = "Sign in on the TerraByte page that opened in your browser."
WAITING_BROWSER_COPY = "Waiting for the browser."
NO_BROWSER_COPY = "Couldn't open your browser. Copy the sign-in link and open it in a browser."
WAITING_SIGN_IN_COPY = "Waiting for you to sign in."


def normalise_code(text: str) -> str:
    """The code as typed, ready to send: upper case without spaces; four letters and four digits typed without the dash
    (or with spaces) get it back ('mpxl3019' -> 'MPXL-3019').  Any other shape is sent as typed, upper-cased: the
    service ignores case, spaces and the dash, and says when a code is wrong."""
    t = "".join((text or "").split()).upper()
    bare = t.replace("-", "")
    if len(bare) == 8 and bare[:4].isalpha() and bare[4:].isdigit():
        return f"{bare[:4]}-{bare[4:]}"
    return t


def open_plugin_manager(iface: Any) -> bool:
    """Open the QGIS Plugin Manager (Plugins > Manage and Install Plugins)."""
    try:
        pmi = iface.pluginManagerInterface()
        if hasattr(pmi, "showPluginManager"):
            pmi.showPluginManager()
            return True
    except Exception:
        pass
    try:
        from qgis.PyQt.QtGui import QAction

        act = iface.mainWindow().findChild(QAction, "mActionManagePlugins")
        if act is not None:
            act.trigger()
            return True
    except Exception:
        pass
    log("could not open the QGIS Plugin Manager")
    return False


class _Main(QWidget):
    """Shared 'Main' column: padding 40/14/16, gap 10."""

    def __init__(self):
        super().__init__()
        self.lay = vbox(self, margins=(14, 40, 14, 16), spacing=10)


@registry.screen("EN-SignedOut")
class SignedOutScreen(Screen):
    def __init__(self, ctx):
        super().__init__(ctx)
        m = _Main()
        outer = vbox(self)
        outer.addWidget(m)
        lay = m.lay
        lay.addWidget(RingIcon(40), 0, Qt.AlignmentFlag.AlignLeft)
        self.heading = label("Lines from your imagery.", h1=True, name="heading")
        self.heading.setContentsMargins(0, 14, 0, 0)
        self.heading.setStyleSheet("font-size:24px;font-weight:500;")
        track(self.heading, -0.48)
        lay.addWidget(self.heading)
        body = label("Road Extractor draws seven line types on your map.", muted=True, name="body")
        body.setStyleSheet("font-size:14px;")
        lay.addWidget(body)
        c = card(name="legendCard")
        cl = vbox(c, margins=(14, 14, 14, 14), spacing=10)
        cl.addWidget(SectionHeading("What it draws"))
        from qgis.PyQt.QtWidgets import QGridLayout

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        for i, (cid, _key, txt, _col) in enumerate(theme.CLASSES):
            cell = QWidget()
            h = hbox(cell, spacing=9 - Swatch.PAD)
            h.addWidget(class_swatch(cid, 18, 3))
            h.addWidget(label(txt, wrap=False))
            h.addStretch(1)
            grid.addWidget(cell, i // 2, i % 2)
            cell.setObjectName(f"lineType{i + 1}")
        cl.addLayout(grid)
        lay.addSpacing(10)
        lay.addWidget(c)
        lay.addStretch(1)
        self.error = label("", danger=True, name="signInError")
        self.error.setVisible(False)
        lay.addWidget(self.error)
        self.sign_in = PillArrowButton("Sign in with TerraByte", name="signInButton")
        self.sign_in.clicked.connect(lambda: self.go("EN-SignInWait"))
        lay.addWidget(self.sign_in)
        helper = label("Sign in opens your browser.", muted=True,
                       name="helper")
        helper.setAlignment(Qt.AlignmentFlag.AlignCenter)
        helper.setStyleSheet("font-size:12px;")
        lay.addWidget(helper)

    def on_show(self, error: str = "", **kw) -> None:
        self.error.setText(error or "")
        self.error.setVisible(bool(error))


@registry.screen("EN-SignInWait")
class SignInWaitScreen(Screen):
    code_delay_ms = CODE_DELAY_MS

    def __init__(self, ctx):
        super().__init__(ctx)
        self._device: Optional[str] = None
        self._url: str = ""
        self._interval_ms = 2000
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.timeout.connect(self._poll)
        self._code_timer = QTimer(self)
        self._code_timer.setSingleShot(True)
        self._code_timer.timeout.connect(self._show_code_card)
        self._inflight = None
        self._gen = 0
        self.no_browser = False
        self._links_on = False
        m = _Main()
        vbox(self).addWidget(m)
        lay = m.lay
        lay.addWidget(RingIcon(40), 0, Qt.AlignmentFlag.AlignLeft)
        h = label("Finish signing in.", h1=True, name="heading")
        h.setStyleSheet("font-size:22px;font-weight:500;")
        track(h, -0.44)
        h.setContentsMargins(0, 14, 0, 0)
        lay.addWidget(h)
        self.body = label(OPENED_COPY, muted=True, name="body")
        self.body.setStyleSheet("font-size:14px;")
        lay.addWidget(self.body)
        strip = QFrame()
        strip.setObjectName("statusStrip")
        strip.setStyleSheet("QFrame#statusStrip{background:rgba(6,182,212,0.08);"
                            "border:1px solid rgba(6,182,212,0.28);border-radius:8px;}")
        sl = hbox(strip, margins=(12, 10, 12, 10), spacing=10)
        sl.addWidget(Spinner())
        self.status = label(WAITING_BROWSER_COPY, name="statusText")
        self.status.setStyleSheet("font-size:12px;")
        sl.addWidget(self.status, 1)
        lay.addWidget(strip)
        self.copy_primary = button("Copy sign-in link", primary=True, name="copyLinkPrimary")
        self.copy_primary.setMinimumHeight(44)
        self.copy_primary.clicked.connect(self.copy_sign_in_link)
        self.copy_primary.setVisible(False)
        lay.addWidget(self.copy_primary)
        links = hbox(spacing=16)
        self.copy_link = button("Copy sign-in link", link=True, name="copyLinkButton")
        self.copy_link.clicked.connect(self.copy_sign_in_link)
        self.open_again = button("Open again", link=True, name="openAgainButton")
        self.open_again.clicked.connect(self.open_sign_in_again)
        links.addWidget(self.copy_link)
        links.addWidget(self.open_again)
        links.addStretch(1)
        lay.addLayout(links)
        self.code_card = card(name="codeCard")
        cl = vbox(self.code_card, margins=(14, 14, 14, 14), spacing=10)
        cl.addWidget(SectionHeading("Still stuck"))
        lb = label("Type the code from your browser", name="codeLabel")
        lb.setStyleSheet("font-weight:500;")
        track(lb, -0.13)
        cl.addWidget(lb)
        row = hbox(spacing=8)
        self.code = QLineEdit()
        self.code.setObjectName("codeInput")
        self.code.setAccessibleName("Code from your browser")
        self.code.setMaxLength(12)
        self.code.setPlaceholderText(CODE_PLACEHOLDER)
        self.code.setMinimumHeight(44)
        self.code.setStyleSheet(f"font-family:{theme.FONT_MONO};font-size:15px;"
                                f"border:1px solid rgba(200,173,255,0.22);border-radius:8px;padding:0 12px;")
        track(self.code, 1.2)
        self.code.textChanged.connect(lambda t: self._upper(t))
        self.code.returnPressed.connect(self.submit_code)
        row.addWidget(self.code, 1)
        self.code_btn = button("Sign in", name="codeSignIn")
        self.code_btn.setMinimumHeight(44)
        self.code_btn.setStyleSheet(f"background:{theme.ACCENT};color:#160d24;font-weight:500;border:none;"
                                    f"padding:0 18px;")
        self.code_btn.clicked.connect(self.submit_code)
        row.addWidget(self.code_btn)
        cl.addLayout(row)
        self.code_error = label("", danger=True, name="codeError")
        self.code_error.setVisible(False)
        cl.addWidget(self.code_error)
        note_row = hbox(spacing=8)
        info_ic = label("", wrap=False)
        info_ic.setContentsMargins(0, 2, 0, 0)
        icons.set_label_icon(info_ic, "info-circle", 14, theme.ACCENT)
        note_row.addWidget(info_ic, 0, Qt.AlignmentFlag.AlignTop)
        note = label("Only type a code from a TerraByte page you opened from here.", muted=True, name="codeNote")
        note.setStyleSheet("font-size:12px;")
        note_row.addWidget(note, 1)
        cl.addLayout(note_row)
        lay.addSpacing(6)
        lay.addWidget(self.code_card)
        lay.addStretch(1)
        self.cancel_btn = button("Cancel", name="cancelButton")
        self.cancel_btn.setAccessibleName("Cancel sign-in")
        self.cancel_btn.setMinimumHeight(44)
        self.cancel_btn.clicked.connect(self.cancel)
        lay.addWidget(self.cancel_btn)

    def _upper(self, t: str) -> None:
        if t != t.upper():
            pos = self.code.cursorPosition()
            self.code.setText(t.upper())
            self.code.setCursorPosition(pos)

    def on_show(self, **kw) -> None:
        self._stop_timers()
        self._gen += 1
        gen = self._gen
        self._device = None
        self._url = ""
        self._set_links_enabled(False)
        self.code_card.setVisible(False)
        self.code.clear()
        self.code_error.setVisible(False)
        self._show_opened(True, force=True)
        self.code_btn.setEnabled(True)

        def ok(res):
            if gen != self._gen:
                return
            self._device = res["device_code"]
            self._interval_ms = int(float(res.get("interval_s", 2)) * 1000)
            self._url = str(res["verification_url"])
            self._set_links_enabled(True)
            self._open_browser()
            self._poll_timer.start(self._interval_ms)
            self._code_timer.start(self.code_delay_ms)

        def fail(err):
            if gen != self._gen:
                return
            self.go("EN-SignedOut", error=_signin_error(err))
        self.ctx.client.auth_device(ok=ok, err=fail)

    def on_hide(self) -> None:
        self._stop_timers()

    def _set_links_enabled(self, on: bool) -> None:
        self._links_on = on
        self.copy_link.setEnabled(on and not self.no_browser)
        self.copy_primary.setEnabled(on and self.no_browser)
        self.open_again.setEnabled(on)

    def _open_browser(self) -> None:
        """Send the browser to the sign-in page and show whether that worked."""
        try:
            opened = self.ctx.open_url(self._url) is not False
        except Exception as e:
            log(f"sign-in: opening the browser failed ({type(e).__name__})")
            opened = False
        self._show_opened(opened)

    def _show_opened(self, opened: bool, force: bool = False) -> None:
        """The wording for a browser that opened, or, when none could be opened, one sentence saying so with
        "Copy sign-in link" as the primary action.  "Open again", Cancel and the waiting stay."""
        if not force and opened == (not self.no_browser):
            return
        self.no_browser = not opened
        self.body.setText(OPENED_COPY if opened else NO_BROWSER_COPY)
        self.status.setText(WAITING_BROWSER_COPY if opened else WAITING_SIGN_IN_COPY)
        self.copy_primary.setVisible(not opened)
        self.copy_link.setVisible(opened)
        self._set_links_enabled(self._links_on)

    def copy_sign_in_link(self) -> None:
        if not self._url:
            return
        from qgis.PyQt.QtGui import QGuiApplication

        cb = QGuiApplication.clipboard()
        cb.setText(self._url)
        from qgis.PyQt.QtCore import QCoreApplication

        QCoreApplication.processEvents()
        if cb.text() == self._url:
            self.status.setText("Link copied. Paste it into a browser.")
        else:
            cb.setText(self._url)
            self.status.setText("Link copied. Paste it into a browser." if cb.text() == self._url
                                else "Couldn't copy the link. Select it and copy it by hand.")

    def open_sign_in_again(self) -> None:
        if self._url:
            self._open_browser()

    def _stop_timers(self) -> None:
        self._poll_timer.stop()
        self._code_timer.stop()

    def _show_code_card(self) -> None:
        self.code_card.setVisible(True)

    def _poll(self) -> None:
        if not self._device:
            return
        gen = self._gen

        def ok(res):
            if gen != self._gen:
                return
            if isinstance(res, dict) and res.get("access_token"):
                self._finish(res)
            else:
                self._poll_timer.start(self._interval_ms)

        def fail(err):
            if gen != self._gen:
                return
            if err.code in ("expired_code",):
                self.go("EN-SignedOut", error=_signin_error(err))
            else:
                self._poll_timer.start(self._interval_ms)
        self.ctx.client.auth_token(self._device, ok=ok, err=fail)

    def submit_code(self) -> None:
        code = normalise_code(self.code.text())
        if not code or not self._device:
            return
        gen = self._gen
        self.code_btn.setEnabled(False)
        self.code_error.setVisible(False)

        def ok(res):
            if gen != self._gen:
                return
            self.code_btn.setEnabled(True)
            if isinstance(res, dict) and res.get("access_token"):
                self._finish(res)

        def fail(err):
            if gen != self._gen:
                return
            self.code_btn.setEnabled(True)
            if err.code == "invalid_code":
                self.code_error.setText(INVALID_CODE_COPY)
                self.code_error.setVisible(True)
            else:
                self.go("EN-SignedOut", error=_signin_error(err))
        self.ctx.client.auth_token(self._device, user_code=code, ok=ok, err=fail)

    def _finish(self, res) -> None:
        self._stop_timers()
        self._gen += 1
        self.ctx.set_session(res["access_token"], res.get("account"))
        self.ctx.bring_qgis_forward()
        self.ctx.load_meta(cb=lambda: self.go("EX-Start"))

    def cancel(self) -> None:
        self._stop_timers()
        self._gen += 1
        self.go("EN-SignedOut")


def _signin_error(err) -> str:
    if err.code == "network_error" or err.status == 0:
        return "Couldn't reach TerraByte. Check your connection and try again."
    return err.message or "Sign-in failed. Try again."


@registry.screen("EN-Update")
class UpdateGateScreen(Screen):
    def __init__(self, ctx):
        super().__init__(ctx)
        m = _Main()
        vbox(self).addWidget(m)
        lay = m.lay
        lay.addWidget(IconTile("icon:download-tray", theme.WARNING, "rgba(245,158,11,0.10)", size=40, radius=10,
                               icon_px=20), 0, Qt.AlignmentFlag.AlignLeft)
        h = label("Update required.", h1=True, name="heading")
        h.setStyleSheet("font-size:22px;font-weight:500;")
        track(h, -0.44)
        h.setContentsMargins(0, 14, 0, 0)
        lay.addWidget(h)
        b = label("This version is too old to extract lines. Your project and layers are untouched.", muted=True, name="body")
        b.setStyleSheet("font-size:14px;")
        lay.addWidget(b)
        grid = QFrame()
        grid.setStyleSheet(f"QFrame#versions{{border-top:1px solid {theme.HAIRLINE};"
                           f"border-bottom:1px solid {theme.HAIRLINE};}}")
        grid.setObjectName("versions")
        gl = hbox(grid, margins=(0, 0, 0, 0), spacing=0)
        self.installed = label("", mono=True, name="installedValue")
        self.needed = label("", mono=True, name="neededValue")
        for cap, val, name in (("Installed", self.installed, "installedCell"), ("Needed", self.needed, "neededCell")):
            cell = QWidget()
            cell.setObjectName(name)
            first = cap == "Installed"
            cv = vbox(cell, margins=(0, 10, 10, 10) if first else (10, 10, 10, 10), spacing=4)
            if not first:
                cell.setStyleSheet(f"QWidget#{name}{{border-left:1px solid {theme.HAIRLINE};}}")
            cv.addWidget(label(cap.upper(), mono=True, name=name + "Label"))
            val.setStyleSheet(f"font-family:{theme.FONT_MONO};font-size:16px;font-weight:500;color:{theme.TEXT};")
            cv.addWidget(val)
            gl.addWidget(cell, 1)
            if cap == "Installed":
                self.needed_cell = None
        self.needed_cell = grid.findChild(QWidget, "neededCell")
        lay.addSpacing(6)
        lay.addWidget(grid)
        callout = QFrame()
        callout.setObjectName("callout")
        callout.setStyleSheet("QFrame#callout{background:rgba(245,158,11,0.10);"
                              "border:1px solid rgba(245,158,11,0.28);border-radius:8px;}")
        cl = hbox(callout, margins=(12, 10, 12, 10), spacing=8)
        warn_ic = label("", wrap=False)
        warn_ic.setContentsMargins(0, 2, 0, 0)
        icons.set_label_icon(warn_ic, "alert-triangle", 14, theme.WARNING)
        cl.addWidget(warn_ic, 0, Qt.AlignmentFlag.AlignTop)
        t = label("Extract is off until you update.",
                  warn=True, name="calloutText")
        t.setStyleSheet(f"font-size:12px;color:{theme.WARNING};")
        cl.addWidget(t, 1)
        lay.addWidget(callout)
        lay.addStretch(1)
        self.update_btn = PillArrowButton("Update", name="updateButton")
        self.update_btn.clicked.connect(self.open_manager)
        lay.addWidget(self.update_btn)
        helper = label("Opens the QGIS Plugin Manager. Restart QGIS when it finishes.", muted=True,
                       name="helper")
        helper.setAlignment(Qt.AlignmentFlag.AlignCenter)
        helper.setStyleSheet("font-size:12px;")
        lay.addWidget(helper)
        self.what = button("What changed", link=True, name="whatChangedLink")
        self.what.clicked.connect(lambda: self.ctx.open_url(self.ctx.url("what_changed_url")))
        lay.addWidget(self.what, 0, Qt.AlignmentFlag.AlignHCenter)

    def on_show(self, **kw) -> None:
        gate = self.ctx.update_gate or {}
        need = gate.get("latest_version") or gate.get("min_version") or ""
        self.installed.setText(plugin_version())
        self.needed.setText(need)
        self.needed_cell.setVisible(bool(need))
        self.update_btn.setText(f"Update to {need}" if need else "Update")
        self.update_btn.update()

    def open_manager(self) -> None:
        open_plugin_manager(self.ctx.iface)


class UpdateCard(QFrame):
    """Non-blocking 'Update available' card for the top of EX-Start.  Hides itself unless the service
    reports a newer version than the installed one (and 'Later' was not pressed this session)."""

    def __init__(self, ctx, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("updateCard")
        self.setProperty("card", True)
        lay = vbox(self, margins=(14, 12, 14, 12), spacing=6)
        lay.addWidget(label("Update available", name="updateCardTitle"))
        self.body = label("", muted=True, name="updateCardBody")
        lay.addWidget(self.body)
        row = hbox(spacing=8)
        self.update_btn = button("Update", primary=True, name="updateCardUpdate")
        self.update_btn.clicked.connect(lambda: open_plugin_manager(ctx.iface))
        self.later_btn = button("Later", ghost=True, name="updateCardLater")
        self.later_btn.clicked.connect(self._later)
        row.addWidget(self.update_btn)
        row.addWidget(self.later_btn)
        row.addStretch(1)
        lay.addLayout(row)
        ctx.meta_changed.connect(self.refresh)
        self.refresh()

    def _later(self) -> None:
        self.ctx.update_later = True
        self.refresh()

    def refresh(self) -> None:
        latest = self.ctx.meta.get("latest_plugin_version")
        have = plugin_version()
        show = bool(latest) and _vkey(latest) > _vkey(have) and not self.ctx.update_later \
            and self.ctx.update_gate is None
        if show:
            self.body.setText(f"{latest} is ready. You have {have}.")
        self.setVisible(show)


def show_update_card(ctx) -> UpdateCard:
    """Factory for EX-Start: ``layout.insertWidget(0, show_update_card(ctx))``."""
    return UpdateCard(ctx)


NOTICE_HEADING = {terms.FIRST: "Before you start", terms.UPDATED: "Our terms have changed"}
NOTICE_CARDS = (
    ("Your imagery is not kept",
     "The area you choose is sent to TerraByte to run the model. The imagery and the lines it returns are deleted "
     "when the run ends."),
    ("Usage is logged", "We log each run (time, area, tile count, model) to track your credits."),
)
NOTICE_NOTE = "You see this once. Declining sends nothing."


def legal_links_html(lead: str = "By continuing you accept the", underline: bool = True) -> str:
    """The given opening words, then "the Terms and the Privacy Policy." with both links (terms.TERMS_URL,
    terms.PRIVACY_URL)."""
    deco = "underline" if underline else "none"
    a = lambda u, t: f'<a href="{u}" style="color:{theme.ACCENT};text-decoration:{deco}">{t}</a>'
    return f"{lead} {a(terms.TERMS_URL, 'Terms')} and the {a(terms.PRIVACY_URL, 'Privacy Policy')}."


class BeforeStartDialog(QDialog):
    def __init__(self, ctx, on_agree: Optional[Callable] = None,
                 on_decline: Optional[Callable] = None, terms_updated: bool = False, parent=None):
        super().__init__(parent or ctx.iface.mainWindow())
        self.ctx = ctx
        self.on_agree, self.on_decline = on_agree, on_decline
        self._settled = False
        self.kind = terms.notice_kind(ctx, legacy_updated=terms_updated)
        self.setObjectName("flowDialog")
        card_dialog(self)
        self.setWindowTitle(NOTICE_HEADING[self.kind])
        self.setModal(True)
        self.setFixedSize(560, 640)
        self.setStyleSheet(theme.base_qss() + f"QDialog#flowDialog{{background:{theme.BG_DIALOG};}}")
        root = vbox(self, spacing=0)
        head = hbox(margins=(24, 22, 16, 18), spacing=14)
        head.addWidget(RingIcon(40), 0, Qt.AlignmentFlag.AlignTop)
        self.heading = label(NOTICE_HEADING[self.kind], h2=True, name="heading")
        track(self.heading, -0.4)
        head.addWidget(self.heading, 1, Qt.AlignmentFlag.AlignVCenter)
        self.close_btn = icon_button("close", "Close and decline", "closeButton")
        self.close_btn.clicked.connect(self.reject)
        head.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)
        root.addWidget(_rule())
        body = vbox(margins=(24, 24, 24, 24), spacing=10)
        for glyph, col_, bg, head_t, text, name in (
                ("upload", "#06b6d4", "rgba(6,182,212,0.12)", NOTICE_CARDS[0][0], NOTICE_CARDS[0][1], "cardUpload"),
                ("grid-2x2", theme.SUCCESS, "rgba(112,223,173,0.12)", NOTICE_CARDS[1][0], NOTICE_CARDS[1][1],
                 "cardUsage")):
            c = card(name=name)
            h = hbox(c, margins=(14, 14, 14, 14), spacing=14)
            h.addWidget(IconTile("icon:" + glyph, col_, bg, size=36, radius=10, icon_px=18), 0,
                        Qt.AlignmentFlag.AlignTop)
            v = vbox(spacing=4)
            ht = label(head_t)
            ht.setStyleSheet("font-size:14px;font-weight:500;")
            track(ht, -0.14)
            v.addWidget(ht)
            bt = label(text, name=name + "Body")
            bt.setStyleSheet(f"font-size:14px;font-weight:{theme.LIGHT};")
            bt.LINE_HEIGHT = 150
            bt.setText(text)
            v.addWidget(bt)
            h.addLayout(v, 1)
            body.addWidget(c)
        legal = label(legal_links_html(), muted=True, name="legal")
        legal.setTextFormat(Qt.TextFormat.RichText)
        legal.setOpenExternalLinks(False)
        legal.linkActivated.connect(ctx.open_url)
        legal.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        legal.setStyleSheet("font-size:12px;")
        legal.setContentsMargins(0, 6, 0, 0)
        link_color(legal, theme.ACCENT)
        body.addWidget(legal)
        self.error = label("", danger=True, name="consentError")
        self.error.setVisible(False)
        body.addWidget(self.error)
        body.addStretch(1)
        root.addLayout(body, 1)
        root.addWidget(_rule())
        foot = hbox(margins=(24, 18, 24, 18), spacing=12)
        note = label(NOTICE_NOTE, muted=True, name="note")
        note.setStyleSheet("font-size:12px;")
        foot.addWidget(note, 1)
        self.decline_btn = button("Decline", name="declineButton")
        self.decline_btn.setMinimumHeight(44)
        self.decline_btn.clicked.connect(self.reject)
        foot.addWidget(self.decline_btn)
        self.agree_btn = PillArrowButton("I agree", height=44, name="agreeButton")
        self.agree_btn.setFixedWidth(150)
        self.agree_btn.clicked.connect(self.agree)
        foot.addWidget(self.agree_btn)
        root.addLayout(foot)

    def agree(self) -> None:
        self.agree_btn.setEnabled(False)
        self.error.setVisible(False)

        def go_consent():
            tv = terms.consent_version(self.ctx)

            def ok(res):
                if res:
                    self.ctx.account = res
                    self.ctx.account_changed.emit()
                self._settled = True
                self.accept()
                if self.on_agree:
                    self.on_agree()

            def fail(err):
                self.agree_btn.setEnabled(True)
                self.error.setText(err.message or "Couldn't record your agreement. Try again.")
                self.error.setVisible(True)
                if err.code == "wrong_terms_version":
                    self.ctx.load_meta()
            self.ctx.client.consent(tv, ok=ok, err=fail)
        if terms.consent_version(self.ctx):
            go_consent()
        else:
            self.ctx.load_meta(cb=go_consent)

    def done(self, r: int) -> None:
        was = self._settled
        self._settled = True
        super().done(r)
        if not was and self.on_decline:
            self.on_decline()


def _rule() -> QFrame:
    f = QFrame()
    f.setProperty("hairline", True)
    return f


@registry.dialog("EN-BeforeStart")
def open_before_start(ctx, **kw) -> QDialog:
    return BeforeStartDialog(ctx, **kw)
