"""Small UI helpers: bridge-command calls, task polling, background threads, config loading."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..bridge import registry
from ..bridge.registry import CommandError
from ..core import paths

ENGINE_MISSING = "Model engine not available in this build (the road-extraction commands are not installed)."


def _import_failure_text() -> str:
    """The real exception of a handler module that failed to import ('' when none did)."""
    errs = registry.load_errors()
    return "; ".join(f"{mod.rsplit('.', 1)[-1]}: {msg}" for mod, msg in sorted(errs.items()))


def call(ctx: Any, name: str, params: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Dispatch a bridge command from the GUI. Returns (result, None) or (None, error message).

    A command that is missing because its handler module failed to import reports that exception, never a
    generic "not available" message."""
    try:
        registry.load_handlers()
        return registry.dispatch(name, params or {}, ctx), None
    except Exception as e:
        code, msg = getattr(e, "code", None), getattr(e, "message", None)
        if code is not None and msg is not None:
            if code == "unknown_command":
                why = _import_failure_text()
                if why:
                    return None, f"{name} is not available because a bridge module failed to load ({why})"
                return None, f"Unknown command {name!r}."
            return None, msg
        return None, f"{type(e).__name__}: {e}"


def command_available(name: str) -> bool:
    try:
        registry.load_handlers()
    except Exception:
        return False
    return name in registry.command_names()


def fmt_km(metres: Any) -> str:
    try:
        return f"{float(metres) / 1000.0:.2f} km"
    except (TypeError, ValueError):
        return "?"


def fmt_size(nbytes: Any) -> str:
    try:
        n = float(nbytes)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return "-"


class TaskPoller(QObject):
    """Polls ``ctx.tasks.status(task_id)`` with a QTimer."""

    updated = pyqtSignal(dict)
    finished = pyqtSignal(dict)

    def __init__(self, ctx: Any, parent: Optional[QObject] = None, interval_ms: int = 250):
        super().__init__(parent)
        self.ctx = ctx
        self.task_id: Optional[str] = None
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    @property
    def active(self) -> bool:
        return self.task_id is not None

    def start(self, task_id: str) -> None:
        self.task_id = task_id
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()
        self.task_id = None

    def _tick(self) -> None:
        if self.task_id is None:
            return
        rec = self.ctx.tasks.status(self.task_id)
        if rec is None:
            self.stop()
            self.finished.emit({"status": "failed", "error": "task not found", "message": "task not found"})
            return
        self.updated.emit(rec)
        if rec["status"] in ("completed", "failed", "cancelled"):
            self.stop()
            self.finished.emit(rec)


class BackgroundCall(QObject):
    """Run ``fn()`` in a Python thread; ``done`` is emitted on the GUI thread with its result."""

    done = pyqtSignal(object)

    def __init__(self, fn: Callable[[], Any], parent: Optional[QObject] = None):
        super().__init__(parent)
        self._fn = fn

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="terrabyte-bg").start()

    def _run(self) -> None:
        try:
            result = self._fn()
        except Exception:
            result = None
        try:
            self.done.emit(result)
        except RuntimeError:
            pass
