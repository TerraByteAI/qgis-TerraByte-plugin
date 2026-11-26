"""Design tokens for the Road Extractor UI flow (colours, radii, fonts) and QSS helpers.

Everything is Qt5/Qt6 safe: only ``qgis.PyQt`` imports, plain stylesheet strings.
"""
from __future__ import annotations

BG_DIALOG = "#121317"
BG_PAGE = "#07080a"
BG_COVER = "#0a0b0e"
BG_BAR = "#0f1014"
TEXT = "#f7f5fb"
TEXT_2 = "#aaa4b5"
TEXT_MUTED = "rgba(247,245,251,0.55)"
TEXT_LABEL = "rgba(247,245,251,0.68)"
ACCENT = "#c8adff"
VIOLET = "#8b5cf6"
DANGER = "#f87171"
DANGER_STRONG = "#ef4444"
SUCCESS = "#70dfad"
WARNING = "#f59e0b"
HAIRLINE = "rgba(247,245,251,0.12)"
BORDER_STRONG = "rgba(247,245,251,0.18)"
CARD_BG = "rgba(247,245,251,0.045)"
ACCENT_BG = "rgba(139,92,246,0.18)"
ACCENT_BG_STRONG = "rgba(139,92,246,0.22)"
BTN_DARK = "#0c0714"

R_PILL = 999
R_CARD = 12
R_DIALOG = 14
R_BTN = 8

FONT_SANS = "'Inter Tight','Inter','Segoe UI','Helvetica Neue',Arial,sans-serif"
FONT_MONO = "'IBM Plex Mono','DejaVu Sans Mono','Consolas','Menlo',monospace"

LINE_CASING = "#101114"
LINE_CASING_PX = 1.0
LINE_PALETTE = [
    (1, "highway", "Highway", "#DC2626", 4.0),
    (2, "local_road", "Local road", "#FFF59D", 2.5),
    (3, "path", "Path", "#FFA040", 2.0),
    (4, "railway", "Railway", "#666666", 2.5),
    (5, "waterway", "Waterway", "#4DABF7", 2.5),
    (6, "runway", "Runway", "#1AA39A", 3.5),
    (7, "power_line", "Power line", "#A0E77D", 1.5),
]
PALE_CLASSES = frozenset({2, 7})
SELECT_HALO = TEXT
SELECT_EDGE = LINE_CASING
SELECT_EDGE_PX = 1.5
SWATCH_OUTLINE_PX = 1

CLASSES = [(cid, key, name, colour) for cid, key, name, colour, _w in LINE_PALETTE]
CLASS_COLOURS = {c[0]: c[3] for c in CLASSES}
CLASS_NAMES = {c[0]: c[2] for c in CLASSES}
CLASS_KEYS = {c[0]: c[1] for c in CLASSES}
LINE_WIDTHS_PX = {c[0]: c[4] for c in LINE_PALETTE}


def class_colour(class_id: int) -> str:
    return CLASS_COLOURS.get(int(class_id), TEXT_2)


def line_width_px(class_id: int) -> float:
    """Width of the coloured line on the map, in screen pixels."""
    return LINE_WIDTHS_PX.get(int(class_id), 2.0)


def casing_width_px(class_id: int) -> float:
    """Width of the dark casing drawn under the line: the line plus 1 px each side."""
    return line_width_px(class_id) + 2 * LINE_CASING_PX


def swatch_needs_outline(class_id: int) -> bool:
    return int(class_id) in PALE_CLASSES


def line_symbol(class_id: int):
    """The map symbol of one line class (a ``QgsLineSymbol``): the dark casing (``LINE_CASING``, ``casing_width_px``)
    under the coloured line (``class_colour``, ``line_width_px``), both in screen pixels with round caps and joins.

    Used for the review layer and the exported layer's style.  The casing draws in rendering pass 0 and the coloured
    lines above it (highways on top), so with symbol levels on every casing is drawn before any line: crossings and
    the joins where a line was cut at a tile edge show no dark seam.  qgis.core is imported here, not at module level,
    so this module stays importable without QGIS."""
    from qgis.core import Qgis, QgsLineSymbol, QgsSimpleLineSymbolLayer
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    ids = [c[0] for c in LINE_PALETTE]
    top = len(ids) - ids.index(int(class_id)) if int(class_id) in ids else 1
    layers = []
    for colour, width, rpass in ((LINE_CASING, casing_width_px(class_id), 0),
                                 (class_colour(class_id), line_width_px(class_id), top)):
        sl = QgsSimpleLineSymbolLayer(QColor(colour), width)
        sl.setWidthUnit(Qgis.RenderUnit.Pixels)
        sl.setPenCapStyle(Qt.PenCapStyle.RoundCap)
        sl.setPenJoinStyle(Qt.PenJoinStyle.RoundJoin)
        sl.setRenderingPass(rpass)
        layers.append(sl)
    return QgsLineSymbol(layers)


try:
    from qgis.PyQt.QtCore import QT_VERSION_STR as _QTV
    _QT5 = _QTV.startswith("5.")
except Exception:
    _QT5 = False
LIGHT = 200 if _QT5 else 300

_FONT_FAMILIES = None
_FONT_IDS: list = []


def unload_fonts() -> None:
    """Release every font this plugin registered (called when the plugin is unloaded)."""
    global _FONT_FAMILIES
    from qgis.PyQt.QtGui import QFontDatabase

    for fid in _FONT_IDS:
        try:
            QFontDatabase.removeApplicationFont(fid)
        except Exception:
            pass
    _FONT_IDS.clear()
    _FONT_FAMILIES = None


def load_fonts() -> list:
    """Register the bundled Inter Tight and IBM Plex Mono (resources/fonts) with Qt, once; returns the families loaded
    (empty when loading failed: the style sheets then fall back to the system fonts in FONT_SANS / FONT_MONO)."""
    global _FONT_FAMILIES
    if _FONT_FAMILIES is not None:
        return _FONT_FAMILIES
    from qgis.PyQt.QtCore import QByteArray
    from qgis.PyQt.QtGui import QFontDatabase

    from ..core import paths

    fams = []
    for f in sorted((paths.PLUGIN_DIR / "resources" / "fonts").glob("*.ttf")):
        try:
            data = QByteArray(f.read_bytes())
        except OSError:
            continue
        fid = QFontDatabase.addApplicationFontFromData(data)
        if fid >= 0:
            _FONT_IDS.append(fid)
            fams += [x for x in QFontDatabase.applicationFontFamilies(fid) if x not in fams]
    _FONT_FAMILIES = fams
    return fams


def base_qss() -> str:
    """Stylesheet applied to the top of every flow widget (panel and dialogs)."""
    from . import icons

    load_fonts()
    tick = icons.checkmark_url()
    return f"""
    QWidget {{ font-family: {FONT_SANS}; font-weight: {LIGHT}; }}
    QDialog#flowDialog, QDialog#stopDialog {{ background: {BG_DIALOG}; border: 1px solid {BORDER_STRONG};
        border-radius: {R_DIALOG}px; color: {TEXT}; font-family: {FONT_SANS}; font-size: 13px; font-weight: {LIGHT}; }}
    QWidget#flowRoot {{ background: {BG_BAR}; color: {TEXT};
        font-family: {FONT_SANS}; font-size: 13px; font-weight: {LIGHT}; }}
    QLabel {{ color: {TEXT}; background: transparent; font-size: 13px; }}
    QLabel[muted="true"] {{ color: {TEXT_2}; }}
    QLabel[mono="true"] {{ font-family: {FONT_MONO}; font-size: 10px; letter-spacing: 1px;
        color: {TEXT_MUTED}; }}
    QLabel[h1="true"] {{ font-size: 24px; font-weight: 500; }}
    QLabel[h2="true"] {{ font-size: 20px; font-weight: 400; }}
    QLabel[danger="true"] {{ color: {DANGER}; }}
    QLabel[ok="true"] {{ color: {SUCCESS}; }}
    QLabel[warn="true"] {{ color: {WARNING}; }}
    QLabel[link="true"] {{ color: {ACCENT}; }}
    QFrame[card="true"] {{ background: {CARD_BG}; border: 1px solid {HAIRLINE};
        border-radius: {R_CARD}px; }}
    QFrame[card="true"][danger="true"] {{ border: 1px solid rgba(239,68,68,0.3); }}
    QFrame[hairline="true"] {{ background: {HAIRLINE}; border: none; max-height: 1px; min-height: 1px; }}
    QPushButton {{ background: transparent; color: {TEXT}; border: 1px solid {BORDER_STRONG};
        border-radius: {R_PILL}px; padding: 6px 16px; font-size: 14px; font-weight: 500; }}
    QPushButton:hover {{ border-color: {ACCENT}; }}
    QPushButton:disabled {{ color: rgba(247,245,251,0.35); border-color: {HAIRLINE}; }}
    QPushButton[primary="true"] {{ background: {TEXT}; color: {BTN_DARK}; border: none;
        font-weight: 500; min-height: 34px; }}
    QPushButton[primary="true"]:disabled {{ background: rgba(247,245,251,0.25);
        color: rgba(12,7,20,0.55); }}
    QPushButton[destructive="true"] {{ background: {DANGER_STRONG}; color: white; border: none; }}
    QPushButton[outlineDanger="true"] {{ color: {DANGER}; border: 1px solid rgba(239,68,68,0.5); }}
    QPushButton[ghost="true"] {{ border: none; color: {TEXT_2}; }}
    QPushButton[link="true"] {{ border: none; color: {TEXT_2}; text-decoration: underline;
        padding: 2px 4px; font-size: 12px; font-weight: {LIGHT}; }}
    QPushButton[chip="true"] {{ background: {ACCENT_BG}; color: {ACCENT}; border: 1px solid rgba(200,173,255,0.35);
        padding: 3px 10px; font-family: {FONT_MONO}; font-size: 11px; }}
    QPushButton[icon="true"] {{ border: none; border-radius: {R_BTN}px; padding: 0; min-width: 32px;
        max-width: 32px; min-height: 32px; max-height: 32px; color: {TEXT_2}; }}
    QPushButton[nav="true"] {{ border: none; border-radius: {R_BTN}px; text-align: left; font-size: 13px;
        padding: 0 10px; min-height: 36px; font-weight: 500; color: {TEXT_2}; }}
    QPushButton[nav="true"][selected="true"] {{ background: {ACCENT_BG_STRONG}; color: {TEXT}; }}
    QLineEdit, QComboBox, QDateEdit, QSpinBox {{ background: rgba(247,245,251,0.06); color: {TEXT};
        border: 1px solid {BORDER_STRONG}; border-radius: {R_BTN}px; padding: 6px 10px;
        selection-background-color: {VIOLET}; }}
    QLineEdit:focus {{ border-color: {ACCENT}; }}
    QComboBox QAbstractItemView {{ background: {BG_DIALOG}; color: {TEXT}; selection-background-color: {VIOLET}; }}
    QCheckBox {{ color: {TEXT}; spacing: 8px; }}
    QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid rgba(247,245,251,0.35); border-radius: 4px;
        background: transparent; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({tick}); }}
    QProgressBar {{ background: rgba(247,245,251,0.10); border: none; border-radius: 3px;
        max-height: 6px; min-height: 6px; }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
    QProgressBar[warn="true"]::chunk {{ background: {WARNING}; }}
    QProgressBar[over="true"]::chunk {{ background: {DANGER}; }}
    QSlider::groove:horizontal {{ height: 4px; background: rgba(247,245,251,0.14); border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: {ACCENT}; }}
    QSlider::groove:horizontal:disabled {{ background: rgba(247,245,251,0.07); }}
    QSlider::sub-page:horizontal:disabled {{ background: rgba(247,245,251,0.10); }}
    QSlider::handle:horizontal:disabled {{ background: rgba(247,245,251,0.18); border: 1px solid rgba(247,245,251,0.12); }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; }}
    QScrollBar::handle:vertical {{ background: rgba(247,245,251,0.2); border-radius: 4px; min-height: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QToolTip {{ background: {BG_DIALOG}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 6px; }}
    """


def mono_caps(text: str) -> str:
    return text.upper()
