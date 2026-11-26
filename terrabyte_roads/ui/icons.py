"""SVG icons from terrabyte_roads/resources/icons, rendered with QSvgRenderer.

``currentColor`` is replaced by the colour you pass, ``rgba()`` paints are turned into rgb + opacity (Qt's SVG renderer
does not read ``rgba()``), and ``stroke`` overrides the stroke width.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from qgis.PyQt.QtCore import QByteArray, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer

from ..core import paths

ICON_DIR = paths.PLUGIN_DIR / "resources" / "icons"

ALIASES = {
    "info": "info-circle", "alert": "alert-triangle", "lock": "padlock", "settings": "gear", "help": "help-circle",
    "polygon": "map-polygon", "x": "close", "retry": "retry-arrow", "up-right": "arrow-up-right",
}

_RGBA = re.compile(r'(fill|stroke)="rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)"')


def available() -> list:
    return sorted(p.stem for p in ICON_DIR.glob("*.svg"))


@lru_cache(maxsize=None)
def _source(name: str) -> str:
    f = ICON_DIR / f"{ALIASES.get(name, name)}.svg"
    if not f.is_file():
        raise KeyError(f"unknown icon {name!r}")
    return f.read_text(encoding="utf-8")


def _prepare(name: str, colour: str, stroke: Optional[float], recolor: bool = False) -> bytes:
    src = _source(name)
    if recolor:
        head, sep, rest = src.partition(">")
        src = re.sub(r'stroke="#[0-9a-fA-F]{3,8}"', 'stroke="currentColor"', head) + sep + rest
    src = _RGBA.sub(lambda m: f'{m.group(1)}="rgb({m.group(2)},{m.group(3)},{m.group(4)})" '
                              f'{m.group(1)}-opacity="{m.group(5)}"', src)
    src = src.replace("currentColor", colour)
    if stroke is not None:
        src = re.sub(r'stroke-width="[^"]*"', f'stroke-width="{stroke:g}"', src, count=1) \
            if 'stroke-width="' in src.split(">", 1)[0] else src
    return src.encode("utf-8")


def _css_colour(colour: str) -> str:
    c = QColor(colour)
    return c.name() if c.isValid() else "#aaa4b5"


def pixmap(name: str, size: int = 16, colour: str = "#aaa4b5", stroke: Optional[float] = None,
           ratio: float = 2.0, width: Optional[int] = None, recolor: bool = False) -> QPixmap:
    """Render icon ``name`` ``size`` px tall (``width`` px wide for non-square icons such as the legend swatches)."""
    r = QSvgRenderer(QByteArray(_prepare(name, _css_colour(colour), stroke, recolor)))
    if not r.isValid():
        raise ValueError(f"icon {name!r} is not a valid SVG")
    w = width or size
    px = QPixmap(int(w * ratio), int(size * ratio))
    px.setDevicePixelRatio(ratio)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p, QRectF(0, 0, w, size))
    p.end()
    return px


def icon(name: str, size: int = 16, colour: str = "#aaa4b5", stroke: Optional[float] = None) -> QIcon:
    return QIcon(pixmap(name, size, colour, stroke))


def set_label_icon(label, name: str, size: int = 16, colour: str = "#aaa4b5", stroke: Optional[float] = None) -> None:
    label.setText("")
    label.setPixmap(pixmap(name, size, colour, stroke))


_tick_path = None


def icon_dir() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / "terrabyte_roads_icons"


def cleanup_icon_dir() -> None:
    """Delete the temp folder of PNGs that style sheets point at and clear their caches (on plugin unload)."""
    import shutil

    global _tick_path
    _tick_path = None
    _png_cache.clear()
    shutil.rmtree(icon_dir(), ignore_errors=True)


def checkmark_url(colour: str = "#160d24") -> str:
    """Path of a small PNG tick for ``QCheckBox::indicator:checked { image: url(...) }`` (QSS cannot draw one)."""
    global _tick_path
    if _tick_path is None or not Path(_tick_path).exists():
        import tempfile

        d = icon_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / "check.png"
        pixmap("check-tight", 15, colour, 2.4, ratio=1.0, recolor=True).save(str(f), "PNG")
        _tick_path = str(f)
    return _tick_path.replace("\\", "/")


_png_cache: dict = {}


def png_url(name: str, size: int = 16, colour: str = "#aaa4b5", stroke: Optional[float] = None) -> str:
    """Path of a PNG render of an icon, for style sheet ``image: url(...)`` (combo-box arrows and the like)."""
    key = (name, size, colour, stroke)
    f = _png_cache.get(key)
    if f is None or not Path(f).exists():
        import tempfile

        d = icon_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = str(d / f"{name}-{size}-{_css_colour(colour).lstrip('#')}-{stroke or 0}.png")
        pixmap(name, size, colour, stroke, ratio=2.0).save(f, "PNG")
        _png_cache[key] = f
    return f.replace("\\", "/")


_png_cache: dict = {}


def png_url(name: str, size: int = 14, colour: str = "#f7f5fb", stroke: Optional[float] = None) -> str:
    """A rendered icon saved as a PNG in the temp folder, for QSS ``image: url(...)`` (combo-box arrows etc.)."""
    key = (name, size, colour, stroke)
    path = _png_cache.get(key)
    if path is None or not Path(path).exists():
        import tempfile

        d = icon_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{ALIASES.get(name, name)}_{size}_{_css_colour(colour).lstrip('#')}.png"
        pixmap(name, size, colour, stroke, ratio=2.0, recolor=True).save(str(f), "PNG")
        path = _png_cache[key] = str(f)
    return path.replace("\\", "/")
