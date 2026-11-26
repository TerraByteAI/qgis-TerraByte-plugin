"""Settings dialog: ST-Account, ST-Billing (+ the sign-out confirm).
One shared dialog shell (960 x 680, 56 px header, 220 px sidebar)."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QPainter
from qgis.PyQt.QtWidgets import (QDialog, QFrame, QPushButton, QWidget)

from ...core import logbuffer
from ...core.numbers import count, counted
from .. import icons, theme
from ..widgets import (card_dialog, Dot, IconTile, Meter, PillArrowButton, SectionHeading, button, card,
                       hairline, hbox, icon_button, label, link_color, track, vbox)
from . import registry
from .controller import log, real_text

HELP_URL = "https://terrabyte.ai"
UPGRADE_URL = "mailto:info@terrabyte.ai?subject=Road%20Extractor%20plan%20upgrade"

LOG_BUFFER = logbuffer.LOG_BUFFER
_collector_installed = False


def install_log_collector() -> None:
    """Also keep lines other code writes straight to the QGIS log (works where the signal is delivered)."""
    global _collector_installed
    if _collector_installed:
        return
    _collector_installed = True

    def on_msg(message: str, tag: str, level) -> None:
        if tag in ("Road Extractor", "TerraByte Roads"):
            logbuffer.record(message)
    try:
        QgsApplication.messageLog().messageReceived[str, str, Qgis.MessageLevel].connect(on_msg)
    except Exception:
        pass


SIDEBAR = [("ST-Account", "Account", "user"), ("ST-Billing", "Plan and tiles", "table-grid")]


class NavButton(QPushButton):
    """A 36 px sidebar row: 16 px design icon (lavender when selected), 13 px/500 label, rounded highlight."""

    def __init__(self, text: str, icon_name: str, name: str = ""):
        super().__init__(text)
        self._icon = icon_name
        self.setObjectName(name)
        self.setFixedHeight(36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("nav", True)
        self.setStyleSheet("QPushButton{border:none;background:transparent;}")

    def paintEvent(self, _e):
        sel = bool(self.property("selected"))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if sel or self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(247, 245, 251, 23 if sel else 10))
            p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        colour = theme.ACCENT if sel else theme.TEXT_2
        p.drawPixmap(10, (self.height() - 16) // 2, icons.pixmap(self._icon, 16, colour))
        f = p.font()
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.Medium)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT if sel else theme.TEXT_2))
        p.drawText(QRectF(36, 0, self.width() - 40, self.height()),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.text())


class SettingsShell(QDialog):
    board = ""
    title = ""
    subtitle = ""
    glyph = "icon:user"

    def __init__(self, ctx, parent=None, pos=None):
        super().__init__(parent or ctx.iface.mainWindow())
        self.ctx = ctx
        if pos is not None:
            self.move(pos)
        self.setObjectName("flowDialog")
        card_dialog(self)
        self.setWindowTitle("Road Extractor settings")
        self.setFixedSize(960, 680)
        self.setStyleSheet(theme.base_qss() + f"QDialog#flowDialog{{background:{theme.BG_DIALOG};"
                           f"border:1px solid {theme.BORDER_STRONG};border-radius:{theme.R_DIALOG}px;}}")
        root = vbox(self, spacing=0)
        head = hbox(margins=(22, 0, 14, 0), spacing=10)
        t = label("Road Extractor settings", h2=True, name="dialogTitle")
        track(t, -0.4)
        head.addWidget(t, 1)
        self.close_btn = icon_button("close", "Close settings", "settingsClose")
        self.close_btn.clicked.connect(self.close)
        head.addWidget(self.close_btn)
        hw = QWidget()
        hw.setFixedHeight(56)
        hw.setLayout(head)
        root.addWidget(hw)
        root.addWidget(hairline())
        body = hbox(spacing=0)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(220)
        sl = vbox(side, margins=(12, 14, 12, 14), spacing=2)
        self.nav: Dict[str, Any] = {}
        for board, text, glyph in SIDEBAR:
            b = NavButton(text, glyph, name=f"nav_{board}")
            b.setProperty("selected", board == self.board)
            b.setAccessibleName(text)
            b.clicked.connect(lambda _=False, bd=board: self.navigate(bd))
            sl.addWidget(b)
            self.nav[board] = b
        sl.addStretch(1)
        body.addWidget(side)
        rule = QFrame()
        rule.setObjectName("sidebarRule")
        rule.setFixedWidth(1)
        rule.setStyleSheet(f"QFrame#sidebarRule{{background:{theme.HAIRLINE};border:none;}}")
        body.addWidget(rule)
        main = QWidget()
        main.setObjectName("settingsMain")
        self.main_lay = vbox(main, margins=(24, 20, 24, 20), spacing=14)
        ph = hbox(spacing=12)
        ph.addWidget(IconTile(self.glyph, icon_px=18))
        col = vbox(spacing=2)
        self.page_title = label(self.title, h2=True, name="pageTitle")
        track(self.page_title, -0.4)
        self.page_sub = label(self.subtitle, muted=True, name="pageSubtitle")
        self.page_sub.setStyleSheet("font-size:12px;")
        col.addWidget(self.page_title)
        col.addWidget(self.page_sub)
        ph.addLayout(col, 1)
        self.main_lay.addLayout(ph)
        self.build_page(self.main_lay)
        body.addWidget(main, 1)
        wrap = QWidget()
        wrap.setLayout(body)
        root.addWidget(wrap, 1)

    def build_page(self, lay) -> None:
        raise NotImplementedError

    def navigate(self, board: str) -> None:
        if board == self.board:
            return
        old = self.pos()
        self.ctx.go(board, pos=old)
        self.ctx.close_dialog(self.board)

    def closeEvent(self, e):
        super().closeEvent(e)


def _row(*, last: bool = False) -> QFrame:
    f = QFrame()
    f.setMinimumHeight(52)
    f.setStyleSheet("" if last else "")
    return f


def _two_line(title: str, helper: str = "", name: str = "") -> QWidget:
    w = QWidget()
    v = vbox(w, spacing=2)
    t = label(title, name=name)
    t.setStyleSheet("font-size:14px;font-weight:500;")
    track(t, -0.14)
    v.addWidget(t)
    if helper:
        h = label(helper, muted=True, name=name + "Helper" if name else "")
        h.setStyleSheet("font-size:12px;")
        v.addWidget(h)
    return w


def _card_with_rows(rows: List[QWidget], danger: bool = False, name: str = "") -> QFrame:
    c = card(danger=danger, name=name)
    v = vbox(c, margins=(0, 0, 0, 0), spacing=0)
    for i, r in enumerate(rows):
        if i:
            v.addWidget(hairline())
        v.addWidget(r)
    return c


def _row_widget(*parts) -> QWidget:
    w = QWidget()
    w.setMinimumHeight(52)
    h = hbox(w, margins=(14, 8, 14, 8), spacing=12)
    for p, stretch in parts:
        h.addWidget(p, stretch)
    return w


class ConfirmDialog(QDialog):
    """Small non-modal confirm: title, body, Cancel / confirm."""

    def __init__(self, ctx, title: str, body: str, confirm_text: str, on_confirm: Callable, parent=None,
                 destructive: bool = False, cancel_name: str = ""):
        super().__init__(parent or ctx.iface.mainWindow())
        self.ctx = ctx
        self.setObjectName("flowDialog")
        card_dialog(self)
        self.setWindowTitle(title)
        self.setFixedWidth(420)
        self.setStyleSheet(theme.base_qss() + f"QDialog#flowDialog{{background:{theme.BG_DIALOG};}}")
        lay = vbox(self, margins=(22, 20, 22, 18), spacing=10)
        lay.addWidget(label(title, h2=True, name="confirmTitle"))
        lay.addWidget(label(body, muted=True, name="confirmBody"))
        row = hbox(spacing=8)
        row.addStretch(1)
        self.cancel_btn = button("Cancel", name="confirmCancel")
        if cancel_name:
            self.cancel_btn.setAccessibleName(cancel_name)
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn = button(confirm_text, destructive=destructive, primary=not destructive, name="confirmOk")
        self.ok_btn.clicked.connect(lambda: (self.accept(), on_confirm()))
        row.addWidget(self.cancel_btn)
        row.addWidget(self.ok_btn)
        lay.addLayout(row)


def open_signout_confirm(ctx, parent=None) -> ConfirmDialog:
    body, lost = "You can sign in again at any time.", False
    rv = getattr(ctx, "review", None)
    try:
        n = len(rv.visible_lines()) if rv is not None and rv.has_lines() else 0
    except Exception:
        n = 0
    if n:
        lost = True
        body = (f"You have {counted(n, 'line', 'lines')} that {'is' if n == 1 else 'are'} not exported yet. "
                "Signing out discards them. They cannot be brought back.")
    dlg = ConfirmDialog(ctx, "Sign out of Road Extractor?", body, "Sign out", lambda: do_sign_out(ctx), parent=parent,
                        destructive=lost, cancel_name="Cancel sign-out")
    ctx.dialogs["ST-SignOutConfirm"] = dlg
    dlg.finished.connect(lambda _r: ctx.dialogs.pop("ST-SignOutConfirm", None))
    dlg.open()
    return dlg


def do_sign_out(ctx) -> None:
    def finish(*_a):
        ctx.sign_out_local()
        ctx.go("EN-SignedOut")
    ctx.client.signout(ok=finish, err=finish)


@registry.dialog("ST-Account")
def open_account(ctx, **kw):
    return AccountDialog(ctx, pos=kw.get("pos"))


class AccountDialog(SettingsShell):
    board = "ST-Account"
    title = "Account"
    subtitle = "Signed in to TerraByte"
    glyph = "icon:user"

    def build_page(self, lay) -> None:
        ctx = self.ctx
        self.avatar = label("", name="avatar")
        self.avatar.setFixedSize(36, 36)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setStyleSheet(f"background:rgba(139,92,246,0.25);color:{theme.ACCENT};border-radius:18px;"
                                  f"font-size:15px;font-weight:500;")
        who = _two_line("", "", name="email")
        self.email = who.findChild(QWidget, "email")
        self.email_helper = label("", muted=True, name="emailHelper")
        self.email_helper.setStyleSheet("font-size:12px;")
        who.layout().addWidget(self.email_helper)
        self.sign_out_btn = button("Sign out", name="signOutButton")
        self.sign_out_btn.setFixedHeight(32)
        self.sign_out_btn.setStyleSheet(f"QPushButton{{background:{theme.CARD_BG};font-size:13px;padding:0 16px;}}")
        self.sign_out_btn.clicked.connect(lambda: open_signout_confirm(ctx, self))
        r1 = _row_widget((self.avatar, 0), (who, 1), (self.sign_out_btn, 0))
        lay.addWidget(_card_with_rows([r1], name="accountCard"))
        lay.addStretch(1)
        from .entry import legal_links_html

        self.legal = label(legal_links_html("Read the"), muted=True, name="legal")
        self.legal.setTextFormat(Qt.TextFormat.RichText)
        self.legal.setOpenExternalLinks(False)
        self.legal.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.legal.linkActivated.connect(ctx.open_url)
        self.legal.setStyleSheet(f"QLabel#legal{{font-size:12px;padding-top:12px;"
                                 f"border-top:1px solid {theme.HAIRLINE};}}")
        link_color(self.legal, theme.ACCENT)
        lay.addWidget(self.legal)
        ctx.account_changed.connect(self.refresh)
        self.refresh()
        if ctx.account is None and not ctx.account_loading:
            ctx.refresh_account()

    def refresh(self) -> None:
        ctx = self.ctx
        if ctx.account is None:
            failed = ctx.account_failed and not ctx.account_loading
            self.avatar.setText("")
            self.email.setText("Signed in with TerraByte" if failed else "Loading your account")
            self.email_helper.setText("Couldn't load your details." if failed else "")
            self.email_helper.setVisible(failed)
            return
        email = real_text((ctx.account or {}).get("email"))
        self.avatar.setText((email[:1] if email else "T").upper())
        self.email.setText(email or "Signed in with TerraByte")
        self.email_helper.setText("Signed in" if email else "Your TerraByte account")
        self.email_helper.setVisible(True)


@registry.dialog("ST-Billing")
def open_billing(ctx, **kw):
    return BillingDialog(ctx, pos=kw.get("pos"))


def _fmt_date(iso: str) -> str:
    """A reset date as the user's local calendar day (``Dec 1, 2026``); the service sends a UTC date or time."""
    import datetime as dt

    try:
        if len(iso) > 10:
            d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if d.tzinfo is not None:
                d = d.astimezone()
        else:
            d = dt.datetime.strptime(iso[:10], "%Y-%m-%d")
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    except (ValueError, TypeError):
        return ""


class BillingDialog(SettingsShell):
    board = "ST-Billing"
    title = "Plan and tiles"
    subtitle = "Your tile balance this month"
    glyph = "icon:table-grid"

    def build_page(self, lay) -> None:
        ctx = self.ctx
        c = card(name="balanceCard")
        cv = vbox(c, margins=(14, 14, 14, 14), spacing=12)
        top = hbox(spacing=16)
        col = vbox(spacing=4)
        self.plan_label = label("", mono=True, wrap=False, name="planLabel")
        self.stat = label("", name="tilesStat")
        self.stat.setStyleSheet("font-size:26px;font-weight:400;")
        track(self.stat, -0.52)
        col.addWidget(self.plan_label)
        col.addWidget(self.stat)
        top.addLayout(col, 1)
        self.upgrade = PillArrowButton("Contact us to upgrade", height=40, name="upgradeButton")
        self.upgrade.setFixedWidth(210)
        self.upgrade.clicked.connect(lambda: ctx.open_url(UPGRADE_URL))
        top.addWidget(self.upgrade, 0, Qt.AlignmentFlag.AlignBottom)
        cv.addLayout(top)
        usage = vbox(spacing=6)
        cv.addLayout(usage)
        self.meter = Meter()
        usage.addWidget(self.meter)
        caps = hbox()
        self.cap_left = label("", muted=True, name="capLeft")
        self.cap_right = label("", muted=True, name="capRight")
        self.cap_right.setAlignment(Qt.AlignmentFlag.AlignRight)
        caps.addWidget(self.cap_left, 1)
        caps.addWidget(self.cap_right, 1)
        self.caps_row = QWidget()
        self.caps_row.setLayout(caps)
        caps.setContentsMargins(0, 0, 0, 0)
        usage.addWidget(self.caps_row)
        rule = hairline()
        cv.addWidget(rule)
        note = label("Plans are arranged by email for now.", muted=True, name="plansNote")
        note.setStyleSheet("font-size:12px;")
        cv.addWidget(note)
        lay.addWidget(c)
        lay.addWidget(SectionHeading("How tiles work"))
        bullets = vbox(spacing=6)
        lay.addLayout(bullets)
        for i, t in enumerate((
                "A tile is one 512 \u00d7 512 pixel piece sent to the model, at the resolution the run uses.",
                "The run check shows the tiles before you start.",
                "Tiles that fail aren't charged.")):
            row = hbox(spacing=10)
            holder = QWidget()
            vbox(holder, margins=(0, 8, 0, 0)).addWidget(Dot(theme.ACCENT, 4))
            row.addWidget(holder, 0, Qt.AlignmentFlag.AlignTop)
            lb = label(t, muted=True, name=f"tilesBullet{i + 1}")
            lb.setStyleSheet("font-size:13px;")
            row.addWidget(lb, 1)
            bullets.addLayout(row)
        lay.addStretch(1)
        ctx.account_changed.connect(self.refresh)
        self.refresh()
        ctx.refresh_account()

    def refresh(self) -> None:
        ctx = self.ctx
        left = ctx.tiles_left()
        tiles = (ctx.account or {}).get("tiles") or {}
        allowance = _positive_int(tiles.get("monthly_allowance"))
        resets = _fmt_date(real_text(tiles.get("resets_on")) or "")
        plan = ctx.plan_name()
        self.plan_label.setText(f"PLAN \u00b7 {plan.upper()}" if plan else "")
        self.plan_label.setVisible(bool(plan))
        if left is None:
            failed = ctx.account_failed and not ctx.account_loading
            self.stat.setText("Couldn't load your tiles." if failed else "Loading your tiles")
        elif left == 0:
            self.stat.setText("You have 0 tiles left this month.")
        else:
            self.stat.setText(f"{counted(left, 'tile', 'tiles')} left this month")
        have_meter = allowance is not None and left is not None
        if have_meter:
            self.meter.set_segments([(max(0.0, min(1.0, left / float(allowance))), theme.ACCENT)])
            self.meter.setAccessibleName(f"{count(left)} of {counted(allowance, 'tile', 'tiles')} left")
        else:
            self.meter.set_segments([])
        self.meter.setVisible(have_meter)
        self.cap_left.setText(f"of {counted(allowance, 'tile', 'tiles')}" if allowance is not None else "")
        self.cap_right.setText(f"Resets on {resets}" if resets else "")
        stale = ctx.account_failed and not ctx.account_loading and left is not None
        if stale:
            self.cap_right.setText("Last known balance: can't reach TerraByte")
        self.caps_row.setVisible(allowance is not None or bool(resets) or stale)


def _positive_int(v: Any) -> Optional[int]:
    """The allowance as a whole number, or None for anything else (missing, zero or negative, text)."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None
