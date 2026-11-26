"""The TerraByte mark (resources/brand/terrabyte.svg) as a pixmap, an icon and a widget."""
from __future__ import annotations

from typing import Dict

from qgis.PyQt.QtCore import QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import QWidget

from ...core import paths

LOGO_PATH = paths.PLUGIN_DIR / "resources" / "brand" / "terrabyte.svg"
_renderer = None
_cache: Dict[int, QPixmap] = {}


def _svg() -> QSvgRenderer:
    global _renderer
    if _renderer is None:
        from qgis.PyQt.QtCore import QByteArray

        _renderer = QSvgRenderer(QByteArray(LOGO_PATH.read_bytes()))
    return _renderer


def _with_glow(img: QImage, std: float) -> QImage:
    """The SVG's neonGlow: a Gaussian blur (std dev ``std`` px) of the mark merged under the mark itself.
    Three box blurs of a down-scaled copy approximate the Gaussian and are cheap at icon sizes."""
    side = img.width()
    glow = img.copy()
    for _ in range(3):
        small = glow.scaled(max(1, side // 2), max(1, side // 2), Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        glow = small.scaled(side, side, Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
    out = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawImage(0, 0, glow)
    p.drawImage(0, 0, img)
    p.end()
    return out


def logo_pixmap(px: int, scale: int = 2, glow: bool = True) -> QPixmap:
    """The mark fitted in a ``px`` square, drawn at ``scale`` times the size for sharp edges."""
    key = (px * 10 + scale) * 2 + int(glow)
    if key not in _cache:
        side = px * scale
        img = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        r = _svg()
        sz = r.defaultSize()
        k = min(side / max(sz.width(), 1), side / max(sz.height(), 1))
        w, h = sz.width() * k, sz.height() * k
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r.render(p, QRectF((side - w) / 2, (side - h) / 2, w, h))
        p.end()
        if glow:
            img = _with_glow(img, 2.5 * scale)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(scale)
        _cache[key] = pm
    return _cache[key]


def logo_icon() -> QIcon:
    ic = QIcon()
    for px in (16, 18, 24, 32, 48):
        ic.addPixmap(logo_pixmap(px))
    return ic


class LogoMark(QWidget):
    """The mark at a fixed size (20 px in a header, 40 px on an entry screen)."""

    def __init__(self, px: int = 40):
        super().__init__()
        self._px = px
        self.setFixedSize(QSize(px, px))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.drawPixmap(0, 0, logo_pixmap(self._px))


class RingIcon(QWidget):
    """The hero icon: a 40 px purple ring (2.5 px, #c8adff) with a 13 px dot (#8b5cf6) in the middle."""

    def __init__(self, px: int = 40):
        super().__init__()
        self.setFixedSize(QSize(px, px))
        self.setObjectName("heroRing")

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        pen = QPen(QColor("#c8adff"))
        pen.setWidthF(2.5)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(1.25, 1.25, w - 2.5, w - 2.5))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#8b5cf6"))
        d = 13.0 * w / 40
        p.drawEllipse(QRectF((w - d) / 2, (w - d) / 2, d, d))
