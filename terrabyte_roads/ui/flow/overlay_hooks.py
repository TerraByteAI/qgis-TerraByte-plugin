"""Hooks a map overlay needs that must be undone when it is destroyed.

``watch_message_bar`` in ``ui/widgets.py`` connects lambdas to the QGIS message bar and never disconnects them, so
once an overlay (or a review) is deleted the bar would call into deleted widgets.  ``MessageBarHook`` is the same idea
with a ``dispose()``; ``SignalGroup`` disconnects any set of signals in one call.  Use both from every overlay and
call ``dispose()`` from its ``clear()`` / ``delete()``.
"""
from __future__ import annotations

from typing import Any, Callable, List, Tuple

from qgis.PyQt.QtCore import QObject, QTimer


class MessageBarHook(QObject):
    """Call ``callback`` when the QGIS message bar shows or drops a message (and once more 400 ms later)."""

    def __init__(self, callback: Callable[[], None], iface: Any = None):
        super().__init__()
        self._cb = callback
        self._bar = None
        self._dead = False
        try:
            if iface is None:
                from qgis.utils import iface as qiface
            else:
                qiface = iface
            mb = qiface.messageBar() if qiface is not None else None
            if mb is not None:
                mb.widgetAdded.connect(self._fire)
                mb.widgetRemoved.connect(self._fire)
                self._bar = mb
        except Exception:
            self._bar = None

    def _call(self) -> None:
        if self._dead:
            return
        try:
            self._cb()
        except RuntimeError:
            self.dispose()

    def _fire(self, *_a) -> None:
        if self._dead:
            return
        self._call()
        QTimer.singleShot(400, self._call)

    def dispose(self) -> None:
        if self._dead:
            return
        self._dead = True
        bar, self._bar = self._bar, None
        if bar is not None:
            for sig in (bar.widgetAdded, bar.widgetRemoved):
                try:
                    sig.disconnect(self._fire)
                except (TypeError, RuntimeError):
                    pass
        self._cb = lambda: None
        try:
            self.deleteLater()
        except RuntimeError:
            pass


class SignalGroup:
    """``connect(signal, slot)`` remembers the pair; ``disconnect_all()`` lets go of every one (safe to repeat)."""

    def __init__(self) -> None:
        self._pairs: List[Tuple[Any, Any]] = []

    def connect(self, signal: Any, slot: Any) -> None:
        signal.connect(slot)
        self._pairs.append((signal, slot))

    def disconnect_all(self) -> None:
        pairs, self._pairs = self._pairs, []
        for sig, slot in pairs:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
