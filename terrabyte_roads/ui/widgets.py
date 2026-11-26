"""Small reusable widgets for the flow screens (cards, chips, meters, headings)."""
from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                                 QListView, QScrollArea, QSizePolicy, QVBoxLayout, QWidget, QComboBox)

from . import theme

DIALOG_HOOKS: list = []


class _Mover(QObject):
    """Lets a frameless dialog be dragged by any part that does not take the mouse itself."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Paint:
            p = QPainter(obj)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor(247, 245, 251, 46), 1))
            p.setBrush(QColor(theme.BG_DIALOG))
            p.drawRoundedRect(QRectF(0.5, 0.5, obj.width() - 1, obj.height() - 1), theme.R_DIALOG, theme.R_DIALOG)
            p.end()
            return False
        if ev.type() == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            wh = obj.windowHandle()
            if wh is not None and hasattr(wh, "startSystemMove"):
                wh.startSystemMove()
        return False


def card_dialog(dlg) -> None:
    """Draw ``dlg`` as the design's borderless card (radius 14, hairline border, translucent corners) instead of a
    window with the window manager's frame; it stays modal, closes with Esc and the card's own x, and can be moved
    by dragging any empty part."""
    dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
    dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    dlg.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    mover = _Mover(dlg)
    dlg._card_mover = mover
    dlg.installEventFilter(mover)
    for hook in list(DIALOG_HOOKS):
        try:
            hook(dlg)
        except Exception:
            pass


def message_bar_inset(canvas) -> int:
    """How far the QGIS message bar reaches down into ``canvas`` (it overlays the top of the map): map pills sit below it."""
    try:
        from qgis.PyQt.QtCore import QPoint
        from qgis.utils import iface as qiface

        mb = qiface.messageBar() if qiface is not None else None
        if mb is None or not mb.isVisible() or mb.height() <= 0:
            return 0
        bottom = mb.mapToGlobal(QPoint(0, mb.height())).y()
        top = canvas.mapToGlobal(QPoint(0, 0)).y()
        return max(0, min(bottom - top, 120))
    except Exception:
        return 0


def track(widget, px: float, caps: bool = False) -> None:
    """Letter spacing in pixels (Qt style sheets have no ``letter-spacing``): the design's 0.08em on mono captions.
    ``caps`` draws the text upper-case (``text-transform: uppercase``) while ``text()`` keeps what was set."""
    f = widget.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px)
    if caps:
        f.setCapitalization(QFont.Capitalization.AllUppercase)
    widget.setFont(f)


def _line_px(widget, pct: int) -> str:
    """``pct`` % of the label's font size as a fixed pixel line height ("20.3px").

    A percentage ``line-height`` in Qt rich text multiplies the font's own line spacing (about 1.3 em for Inter Tight),
    so 145 % gives about 1.9 em where CSS ``line-height: 1.45`` means 1.45 em.  A pixel value is exact."""
    px = widget.font().pixelSize()
    if px <= 0:
        px = max(1, round(widget.fontInfo().pixelSize()))
    return f"{px * pct / 100:.1f}px"


class ParagraphLabel(QLabel):
    """A wrapping label drawn with the design's ``line-height: 1.45`` (Qt style sheets have no line-height, rich
    text does).

    ``text()`` keeps returning the plain string that was set; text that already is HTML is left alone.  ``LINE_HEIGHT``
    (percent of the font size) can be overridden per instance, e.g. 150 for the consent dialog's paragraphs."""

    LINE_HEIGHT = 145

    def __init__(self, text: str = ""):
        super().__init__("")
        self._raw = ""
        self._lh = True
        self._applied = ""
        self.setText(text)

    def setLineHeight(self, on: bool) -> None:
        self._lh = on
        self.setText(self._raw)

    def setText(self, text: str) -> None:
        self._raw = text or ""
        if self._lh and self.wordWrap() and self._raw and "<" not in self._raw:
            import html

            body = html.escape(self._raw).replace("\n", "<br>")
            self._applied = _line_px(self, self.LINE_HEIGHT)
            super().setText(f'<div style="line-height:{self._applied}">{body}</div>')
        else:
            self._applied = ""
            super().setText(self._raw)

    def text(self) -> str:
        return self._raw

    def setWordWrap(self, on: bool) -> None:
        super().setWordWrap(on)
        self.setText(self._raw)

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if self._applied and e.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) \
                and _line_px(self, self.LINE_HEIGHT) != self._applied:
            self.setText(self._raw)


class MonoNumberLabel(QLabel):
    """A label whose numbers are set in white IBM Plex Mono (the design's ``<b>`` figures); ``text()`` stays plain."""

    def __init__(self, text: str = "", wrap: bool = False):
        super().__init__("")
        self._raw = ""
        self.setWordWrap(wrap)
        self.setText(text)

    def setText(self, text: str) -> None:
        import html
        import re

        self._raw = text or ""
        body = html.escape(self._raw)
        body = re.sub(r"(\d[\d,.]*)", lambda m: f'<span style="font-family:{theme.FONT_MONO};color:{theme.TEXT};">'
                                                  f'{m.group(1)}</span>', body)
        super().setText(f'<div style="line-height:{_line_px(self, ParagraphLabel.LINE_HEIGHT)}">{body}</div>'
                        if self.wordWrap() else body)

    def text(self) -> str:
        return self._raw


def label(text: str = "", *, muted=False, mono=False, h1=False, h2=False, danger=False, ok=False,
          warn=False, wrap=True, name: str = "") -> QLabel:
    if wrap and not (mono or h1 or h2):
        lb = ParagraphLabel("")
        lb.setWordWrap(True)
        lb.setText(text)
    else:
        lb = QLabel(text)
        lb.setWordWrap(wrap)
    for prop, on in (("muted", muted), ("mono", mono), ("h1", h1), ("h2", h2), ("danger", danger),
                     ("ok", ok), ("warn", warn)):
        if on:
            lb.setProperty(prop, True)
    if name:
        lb.setObjectName(name)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    if mono:
        track(lb, 0.8)
    return lb


def link_color(lb: QLabel, color: str) -> None:
    """Colour of the links inside a rich-text label.  Qt 5 ignores an inline ``color`` on ``<a>`` and uses the
    palette's Link role, Qt 6 honours both, so the palette is set as well."""
    pal = lb.palette()
    pal.setColor(QPalette.ColorRole.Link, QColor(color))
    lb.setPalette(pal)


class PillButton(QPushButton):
    """A QPushButton whose corner radius follows half its height.

    Qt style sheets ignore a ``border-radius`` larger than half the widget's size (so ``999px`` draws a
    rectangle); the radius is therefore re-applied on resize, merged after whatever sheet the caller set."""

    def __init__(self, text: str = ""):
        super().__init__(text)
        self._user_sheet = ""
        self._radius = -1

    def setStyleSheet(self, sheet: str) -> None:
        sheet = sheet or ""
        if sheet and "{" not in sheet:
            sheet = f"QPushButton{{{sheet}}}"
        self._user_sheet = sheet
        self._apply()

    def _apply(self) -> None:
        r = max(self.height(), 24) // 2
        self._radius = r
        super().setStyleSheet(f"{self._user_sheet}\nQPushButton{{border-radius:{r}px;}}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if max(self.height(), 24) // 2 != self._radius:
            self._apply()


def button(text: str, *, primary=False, ghost=False, destructive=False, outline_danger=False,
           link=False, chip=False, name: str = "", on_click: Optional[Callable] = None) -> QPushButton:
    b = QPushButton(text) if (link or ghost) else PillButton(text)
    for prop, on in (("primary", primary), ("ghost", ghost), ("destructive", destructive),
                     ("outlineDanger", outline_danger), ("link", link), ("chip", chip)):
        if on:
            b.setProperty(prop, True)
    if name:
        b.setObjectName(name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click is not None:
        b.clicked.connect(lambda _=False: on_click())
    return b


def set_tip(w: QWidget, text: str) -> None:
    """A control's hover text, given to assistive tools as its accessible description too."""
    w.setToolTip(text)
    w.setAccessibleDescription(text)


def name_combo(combo: QComboBox, name: str, tip: str = "") -> None:
    """A combo box's accessible name, and its hover text if any.  Qt on macOS and Linux hands assistive tools
    a combo box's current text as its name whatever ``accessibleName`` says (QAccessibleComboBox), and the description
    as it is, so the description starts with the name: "Format", "Imagery. Pick a raster layer from the project"."""
    combo.setAccessibleName(name)
    if tip:
        combo.setToolTip(tip)
    combo.setAccessibleDescription(f"{name}. {tip}" if tip else name)


def card(*, danger=False, name: str = "") -> QFrame:
    f = QFrame()
    f.setProperty("card", True)
    if danger:
        f.setProperty("danger", True)
    if name:
        f.setObjectName(name)
    return f


def hairline() -> QFrame:
    f = QFrame()
    f.setProperty("hairline", True)
    f.setFrameShape(QFrame.Shape.NoFrame)
    return f


def vbox(parent: Optional[QWidget] = None, margins=(0, 0, 0, 0), spacing=10) -> QVBoxLayout:
    lay = QVBoxLayout(parent) if parent is not None else QVBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return lay


def hbox(parent: Optional[QWidget] = None, margins=(0, 0, 0, 0), spacing=10) -> QHBoxLayout:
    lay = QHBoxLayout(parent) if parent is not None else QHBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return lay


def icon_button(name: str, tooltip: str = "", obj: str = "", size: int = 16, colour: str = theme.TEXT_2,
                box: int = 32, radius: int = 8, stroke: Optional[float] = None) -> QPushButton:
    """A flat square icon button (the design's 32 px close / gear buttons) drawn with the design SVG."""
    from qgis.PyQt.QtCore import QSize

    from . import icons

    b = QPushButton()
    if obj:
        b.setObjectName(obj)
    b.setIcon(icons.icon(name, size, colour, stroke))
    b.setIconSize(QSize(size, size))
    b.setFixedSize(box, box)
    if tooltip:
        b.setToolTip(tooltip)
        b.setAccessibleName(tooltip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setStyleSheet(f"QPushButton{{background:transparent;border:none;border-radius:{radius}px;}}"
                    f"QPushButton:hover{{background:rgba(247,245,251,0.06);}}")
    return b


class ElideLabel(QLabel):
    """A one-line label that ends in an ellipsis instead of being cut off (full text in the tooltip)."""

    def __init__(self, text: str = "", name: str = ""):
        super().__init__("")
        self._full = text
        if name:
            self.setObjectName(name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        w = max(10, self.width())
        super().setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, w))


class Dot(QWidget):
    """A small round bullet (the list bullets on ST-Billing).  Section and card titles have no dot."""

    def __init__(self, colour: str = theme.VIOLET, size: int = 7):
        super().__init__()
        self._c = QColor(colour)
        self.setFixedSize(size, size)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._c)
        p.drawEllipse(QRectF(0, 0, self.width(), self.height()))


class SectionHeading(QWidget):
    """Mono uppercase section / card title (11 px, 0.08em letter spacing), starting at the left edge.

    There is no indicator dot before it; ``colour`` is accepted for compatibility with existing callers and not used.
    ``danger`` draws the title in the danger colour."""

    def __init__(self, text: str, colour: str = theme.VIOLET, danger: bool = False):
        super().__init__()
        lay = hbox(self, spacing=10)
        self.text = label(text.upper(), mono=True, wrap=False)
        self.text.setStyleSheet(f"color: {theme.DANGER if danger else theme.TEXT_LABEL};"
                                f" font-family: {theme.FONT_MONO}; font-size: 11px; font-weight: 400;")
        track(self.text, 0.88)
        lay.addWidget(self.text)
        lay.addStretch(1)


class Swatch(QWidget):
    """A line swatch: a ``w`` x ``h`` px rounded bar in a line colour.

    ``outline`` draws a 1 px dark ring (``theme.LINE_CASING``, CSS ``box-shadow: 0 0 0 1px``) around the bar;
    the two pale line colours have it (``theme.swatch_needs_outline``).  Every swatch keeps that 1 px margin
    (``PAD``) whether or not it is outlined, so rows with and without the ring line up: the widget is
    ``(w + 2) x (h + 2)`` and callers take ``PAD`` off the spacing next to it.  Build class swatches with
    ``class_swatch``.  ``dashed`` draws an outlined dashed square instead (a tile that stopped part way)."""

    PAD = theme.SWATCH_OUTLINE_PX

    def __init__(self, colour: str, w: int = 18, h: int = 3, dashed: bool = False, outline: bool = False):
        super().__init__()
        self._c = QColor(colour)
        self._dashed = dashed
        self.outline = bool(outline)
        self.bar_size = (w, h)
        self.class_id: Optional[int] = None
        self.setFixedSize(w + 2 * self.PAD, h + 2 * self.PAD)

    def colour(self) -> str:
        return self._c.name()

    def bar_rect(self) -> QRectF:
        return QRectF(self.PAD, self.PAD, *self.bar_size)

    def paintEvent(self, _e):
        p = QPainter(self)
        if self._dashed:
            pen = QPen(self._c, 1.4)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.bar_rect())
            return
        p.setPen(Qt.PenStyle.NoPen)
        bar = self.bar_rect()
        if self.outline:
            p.setBrush(QColor(theme.LINE_CASING))
            o = self.PAD
            p.drawRoundedRect(bar.adjusted(-o, -o, o, o), 1.5 + o, 1.5 + o)
        p.setBrush(self._c)
        p.drawRoundedRect(bar, 1.5, 1.5)


def class_swatch(class_id: int, w: int = 18, h: int = 3) -> Swatch:
    """The swatch of one line class, from the palette in theme.py: its colour, and the dark ring for a pale one."""
    sw = Swatch(theme.class_colour(class_id), w, h, outline=theme.swatch_needs_outline(class_id))
    sw.class_id = int(class_id)
    sw.setObjectName(f"swatch_{int(class_id)}")
    return sw


class Meter(QWidget):
    """Horizontal meter. segments = [(fraction_of_total, colour)]; total scales the bar."""

    def __init__(self, height: int = 6):
        super().__init__()
        self.segments = []
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_segments(self, segments) -> None:
        self.segments = list(segments)
        self.update()

    def paintEvent(self, _e):
        """The meter: a rounded track (rgba .08), plain segments clipped to it, and a 2 px page-colour
        rule in front of every segment after the first."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        track = QPainterPath()
        track.addRoundedRect(r, self.height() / 2, self.height() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.fillPath(track, QColor(247, 245, 251, 20))
        p.setClipPath(track)
        x = 0.0
        for k, (frac, colour) in enumerate(self.segments):
            w = max(0.0, min(1.0, frac)) * r.width()
            if w <= 0:
                continue
            p.setBrush(QColor(colour))
            p.drawRect(QRectF(x, 0, w, r.height()))
            if k:
                p.setBrush(QColor(theme.BG_COVER))
                p.drawRect(QRectF(x, 0, 2, r.height()))
            x += w


class IconTile(QLabel):
    """Square tinted tile with one glyph (36 px for settings pages, 40 px for entry screens)."""

    def __init__(self, glyph: str, colour: str = theme.ACCENT, bg: str = theme.ACCENT_BG, size: int = 36,
                 radius: int = 8, icon_px: int = 0, stroke: float = 0):
        super().__init__("" if glyph.startswith("icon:") else glyph)
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"background:{bg}; color:{colour}; border-radius:{radius}px; font-size:{size // 2}px;")
        if glyph.startswith("icon:"):
            from . import icons

            icons.set_label_icon(self, glyph[5:], icon_px or size // 2 + 2, colour, stroke or None)


class Spinner(QWidget):
    """14 px rotating arc."""

    def __init__(self, size: int = 14):
        super().__init__()
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(60)

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(200, 173, 255, 72), 2))
        p.drawEllipse(r)
        p.setPen(QPen(QColor(theme.ACCENT), 2))
        p.drawArc(r, -self._angle * 16, 90 * 16)


class PillArrowButton(PillButton):
    """Primary pill with the label on the left and a dark round chip with an up-right arrow on the right."""

    def __init__(self, text: str, height: int = 48, name: str = ""):
        super().__init__(text)
        self._h = height
        self.setFixedHeight(height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if name:
            self.setObjectName(name)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.4)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.TEXT))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        chip = self._h - 14 if self._h >= 44 else self._h - 12
        m = (self._h - chip) / 2
        cr = QRectF(r.right() - m - chip, m, chip, chip)
        p.setBrush(QColor(theme.BTN_DARK))
        p.drawEllipse(cr)
        from . import icons

        ic = 14 if chip >= 32 else 12
        px = icons.pixmap("arrow-up-right", ic, theme.TEXT, 2.0)
        p.drawPixmap(int(cr.center().x() - ic / 2), int(cr.center().y() - ic / 2), px)
        p.setPen(QColor(theme.BTN_DARK))
        f = p.font()
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.Medium)
        p.setFont(f)
        p.drawText(QRectF(20 if self._h >= 44 else 18, 0, r.width() - chip - 34, r.height()),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.text())


class Screen(QWidget):
    """Base class of every flow screen.  Subclasses set the screen's registry key (e.g. ``EX-Start``) and
    implement ``set_state``."""

    board = ""
    navigate = pyqtSignal(str, dict)

    def __init__(self, ctx, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName(f"screen_{self.board}")

    def go(self, board: str, **kw) -> None:
        self.ctx.go(board, **kw)

    def on_show(self, **kw) -> None:
        """Called each time the screen is shown (refresh data here)."""

    def on_hide(self) -> None:
        pass


class ArrowButton(PillButton):
    """Full-width pill with the label on the left and a round dark chip carrying an arrow on the right
    (the design's primary / 'Start' button).  ``primary`` False draws the accent variant."""

    def __init__(self, text: str, arrow: str = "arrow-up-right", primary: bool = True):
        super().__init__(text)
        self._arrow = arrow
        self.setProperty("primary", True if primary else False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(48)
        self.setStyleSheet("QPushButton{text-align:left; padding:0 50px 0 20px; font-size:14px; font-weight:500;"
                           " min-height:46px; border:1px solid transparent; border-radius:24px;}")

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        d = 34
        r = QRectF(self.width() - d - 7, (self.height() - d) / 2, d, d)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.BTN_DARK) if self.isEnabled() else QColor(247, 245, 251, 30))
        p.drawEllipse(r)
        from . import icons

        ic = icons.pixmap(self._arrow, 14, theme.TEXT if self.isEnabled() else "#8a8693", 2.0)
        p.drawPixmap(int(r.center().x() - 7), int(r.center().y() - 7), ic)


def pill_button(text: str, *, accent: bool = False, name: str = "") -> QPushButton:
    """Secondary pill (outlined); ``accent`` = lavender fill."""
    b = PillButton(text)
    b.setMinimumHeight(48)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if accent:
        b.setStyleSheet(f"QPushButton{{background:{theme.ACCENT}; color:#160d24; border:1px solid transparent;"
                        f" border-radius:24px; font-weight:500; font-size:14px;}}")
    else:
        b.setStyleSheet(f"QPushButton{{background:{theme.CARD_BG}; color:{theme.TEXT};"
                        f" border:1px solid {theme.BORDER_STRONG}; border-radius:24px; font-weight:500; font-size:14px;}}"
                        f"QPushButton:disabled{{color:rgba(247,245,251,0.55); background:rgba(247,245,251,0.05);"
                        f" border-color:{theme.HAIRLINE};}}")
    if name:
        b.setObjectName(name)
    return b



MIN_VIEW_H = 160
FOCUS_MARGIN = 12


class FitScrollArea(QScrollArea):
    """A scrolling page whose content is never squeezed below its real height.

    Word-wrapped labels report a small minimum height until they know their width, so a plain QScrollArea can let the
    page shrink to the window and draw text over the next card (e.g. at 150% scaling on a 1080p screen).  Here the
    page's minimum height follows its layout's height for the width it has, and the scroll bar shows when it is needed.

    QGIS can squeeze the panel (its Identify Results dock opens under it).  The body never gets less than
    ``MIN_VIEW_H`` (so QGIS gives the other dock less), its content scrolls and the screen's button bar stays below it,
    and a control in the body that takes the keyboard focus (Tab, a click, an assistive tool) is scrolled fully into
    view: a control scrolled out of sight still has its full frame in the accessibility tree, under the button bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMinimumHeight(MIN_VIEW_H)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focus_moved)

    def _focus_moved(self, _old, new) -> None:
        try:
            w = self.widget()
            if w is not None and isinstance(new, QWidget) and w.isAncestorOf(new):
                self.ensureWidgetVisible(new, FOCUS_MARGIN, FOCUS_MARGIN)
        except RuntimeError:
            pass

    def setWidget(self, w) -> None:
        super().setWidget(w)
        w.installEventFilter(self)

    def _fit(self) -> None:
        w = self.widget()
        if w is None or w.layout() is None:
            return
        width = max(self.viewport().width(), 1)
        h = w.layout().totalHeightForWidth(width)
        if h > 0 and h != w.minimumHeight():
            w.setMinimumHeight(h)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def showEvent(self, e):
        super().showEvent(e)
        self._fit()

    def event(self, e):
        r = super().event(e)
        if e.type() == QEvent.Type.LayoutRequest:
            self._fit()
        return r

    def eventFilter(self, o, e):
        r = super().eventFilter(o, e)
        if o is self.widget() and e.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Resize):
            self._fit()
        return r


SCROLLBAR_QSS = (
    "QScrollBar:vertical{background:rgba(247,245,251,0.05); width:10px; margin:0; border-radius:5px;}"
    "QScrollBar::handle:vertical{background:rgba(247,245,251,0.32); border-radius:5px; min-height:32px;}"
    "QScrollBar::handle:vertical:hover{background:rgba(247,245,251,0.5);}"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical{height:0; width:0;}"
    "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical{background:transparent;}"
)


def style_combo_popup(combo: QComboBox) -> None:
    """The drop-down list is dark and framed like the panel, not the white-framed native Windows list."""
    view = QListView(combo)
    view.setUniformItemSizes(True)
    combo.setView(view)
    view.setStyleSheet(
        f"QListView{{background:{theme.BG_DIALOG}; color:{theme.TEXT}; border:none; outline:0; padding:4px;"
        f" font-size:13px;}}"
        f"QListView::item{{min-height:30px; padding:0 10px; border-radius:6px;}}"
        f"QListView::item:selected, QListView::item:hover{{background:{theme.VIOLET}; color:#ffffff;}}")
    box = view.window()
    if box is not combo.window():
        box.setStyleSheet(f"background:{theme.BG_DIALOG}; border:1px solid {theme.BORDER_STRONG}; border-radius:8px;")
