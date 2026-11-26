"""Mail contact helpers: is a mail app installed, copy an address, and a line that shows the address."""
from __future__ import annotations

import sys
from urllib.parse import unquote

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

from .. import theme


def address_of(url: str) -> str:
    u = str(url)
    if u.lower().startswith("mailto:"):
        u = u[7:]
    return unquote(u.split("?", 1)[0])


def copy_address(url: str) -> str:
    addr = address_of(url)
    cb = QApplication.clipboard()
    if cb is not None:
        cb.setText(addr)
    return addr


def mail_handler_exists() -> bool:
    """Whether a ``mailto:`` link will reach a mail app.  Windows may have no mail app at all, and then ShellExecute
    shows its own "get a new app" page, so the registry is checked; elsewhere the desktop answers for itself and this
    returns True."""
    if sys.platform != "win32":
        return True
    try:
        import winreg

        for root, sub in ((winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice"),
                          (winreg.HKEY_CLASSES_ROOT, r"mailto\shell\open\command")):
            try:
                with winreg.OpenKey(root, sub):
                    return True
            except OSError:
                continue
    except Exception:
        return True
    return False


class ContactLine(QWidget):
    """``info@terrabyte.ai`` as selectable text with a Copy button; shown beside every button that opens a mail."""

    def __init__(self, email: str, parent=None, name: str = "contactLine"):
        super().__init__(parent)
        self.setObjectName(name)
        self.email = email
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.addr = QLabel(email)
        self.addr.setObjectName("contactAddress")
        self.addr.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.addr.setStyleSheet(f"font-family:{theme.FONT_MONO}; font-size:12px; color:{theme.TEXT_2}; background:transparent; border:none;")
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("contactCopy")
        self.copy_btn.setAccessibleName("Copy email address")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setFlat(True)
        self.copy_btn.setStyleSheet(f"QPushButton{{border:none; background:transparent; color:{theme.ACCENT}; font-size:12px; padding:0 2px;}}"
                                    f"QPushButton:hover{{color:{theme.TEXT};}}")
        self.copy_btn.clicked.connect(self.copy)
        lay.addWidget(self.addr)
        lay.addWidget(self.copy_btn)
        lay.addStretch(1)

    def copy(self) -> None:
        copy_address(self.email)
        self.copy_btn.setText("Copied")
        self.copy_btn.setAccessibleDescription("Copied")
