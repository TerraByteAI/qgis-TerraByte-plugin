"""Background tasks (QgsTask) with an ID-based registry for MCP polling.

Long operations (road extraction, imagery download) run as ``ManagedTask`` subclasses
in the QGIS task manager so the GUI and the MCP bridge stay responsive. MCP tools
return a ``task_id`` immediately; clients poll ``get_task_status`` and may
``cancel_task``.

Threading rules (PyQGIS):
* ``work()`` runs in a worker thread: it must NOT touch QgsProject, layers, iface or
  any QWidget. Use plain data (paths, WKT, numbers), GDAL/OGR, NumPy.
  Call ``self.report(fraction, message)`` for progress and ``self.check_cancelled()``
  regularly (raises TaskCancelled).
* ``on_success(result)`` runs on the main thread after ``work()`` succeeds; it may add
  layers to the project and returns the final (JSON-safe) result summary.

The manager keeps a JSON-safe *record* per task that is updated as the task runs, so
status queries never touch the underlying C++ QgsTask (which QGIS deletes once it
finishes - accessing it afterwards raises RuntimeError).
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional

from qgis.core import QgsApplication, QgsTask

from .types import TaskCancelled

STATUSES = ("queued", "running", "completed", "failed", "cancelled")
_MAX_RECORDS = 200


class ManagedTask(QgsTask):
    """Base class. Subclasses implement ``work()`` and optionally ``on_success()``."""

    kind = "task"

    def __init__(self, description: str, can_cancel: bool = True):
        flags = QgsTask.Flag.CanCancel if can_cancel else QgsTask.Flags()
        super().__init__(description, flags)
        self.task_id = uuid.uuid4().hex[:12]
        self.record: Dict[str, Any] = {
            "task_id": self.task_id,
            "kind": self.kind,
            "description": description,
            "status": "queued",
            "progress": 0.0,
            "message": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "runtime_s": None,
            "result": None,
            "error": None,
        }
        self._result: Any = None
        self._error_tb: Optional[str] = None

    def work(self) -> Any:
        raise NotImplementedError

    def on_success(self, result: Any) -> Dict[str, Any]:
        """Main thread. Default: return ``result`` unchanged (must be JSON-safe)."""
        return result

    def report(self, fraction: float, message: str = "") -> None:
        pct = max(0.0, min(100.0, float(fraction) * 100.0))
        self.record["progress"] = round(pct, 1)
        if message:
            self.record["message"] = message
        self.setProgress(pct)

    def check_cancelled(self) -> None:
        if self.isCanceled():
            raise TaskCancelled()

    def is_cancelled(self) -> bool:
        return self.isCanceled()

    def run(self) -> bool:
        self.record["status"] = "running"
        self.record["started_at"] = time.time()
        self.record["message"] = "running"
        try:
            self._result = self.work()
            return not self.isCanceled()
        except TaskCancelled:
            return False
        except Exception as e:
            self.record["error"] = f"{type(e).__name__}: {e}"
            self._error_tb = traceback.format_exc()
            return False

    def finished(self, ok: bool) -> None:
        rec = self.record
        rec["finished_at"] = time.time()
        if rec["started_at"]:
            rec["runtime_s"] = round(rec["finished_at"] - rec["started_at"], 2)
        if ok:
            try:
                summary = self.on_success(self._result)
                if isinstance(summary, dict) and rec["runtime_s"] is not None:
                    summary.setdefault("runtime_s", rec["runtime_s"])
                rec["result"] = summary
                rec["status"] = "completed"
                rec["progress"] = 100.0
                rec["message"] = "completed"
            except Exception as e:
                rec["status"] = "failed"
                rec["error"] = f"{type(e).__name__}: {e}"
                self._error_tb = traceback.format_exc()
                rec["message"] = "failed while loading results"
        elif self.isCanceled():
            rec["status"] = "cancelled"
            rec["message"] = "cancelled"
        else:
            rec["status"] = "failed"
            rec["message"] = "failed"
            if not rec["error"]:
                rec["error"] = "task failed"
        if self._error_tb:
            try:
                from qgis.core import Qgis, QgsMessageLog

                QgsMessageLog.logMessage(self._error_tb, "Road Extractor", Qgis.MessageLevel.Warning)
            except Exception:
                pass
        self._result = None


class TaskManager:
    """Registry of ManagedTasks by ID. Create one per plugin instance (main thread)."""

    def __init__(self) -> None:
        self._tasks: Dict[str, ManagedTask] = {}
        self._records: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, task: ManagedTask) -> str:
        with self._lock:
            self._tasks[task.task_id] = task
            self._records[task.task_id] = task.record
            self._prune()
        QgsApplication.taskManager().addTask(task)
        return task.task_id

    def status(self, task_id: str) -> Optional[Dict[str, Any]]:
        rec = self._records.get(task_id)
        if rec is None:
            return None
        out = dict(rec)
        if out["status"] == "running" and out["started_at"]:
            out["elapsed_s"] = round(time.time() - out["started_at"], 1)
        out.pop("created_at", None)
        return out

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        rec = self._records.get(task_id)
        if task is None or rec is None or rec["status"] not in ("queued", "running"):
            return False
        try:
            task.cancel()
        except RuntimeError:
            return False
        return True

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        recs = sorted(self._records.values(), key=lambda r: r["created_at"], reverse=True)
        return [{k: r[k] for k in ("task_id", "kind", "description", "status", "progress")}
                for r in recs[:limit]]

    def active_count(self) -> int:
        return sum(1 for r in self._records.values() if r["status"] in ("queued", "running"))

    def cancel_all(self) -> None:
        for tid in list(self._tasks):
            self.cancel(tid)

    def _prune(self) -> None:
        if len(self._records) <= _MAX_RECORDS:
            return
        done = sorted((r for r in self._records.values() if r["status"] not in ("queued", "running")),
                      key=lambda r: r["created_at"])
        for r in done[: len(self._records) - _MAX_RECORDS]:
            self._records.pop(r["task_id"], None)
            self._tasks.pop(r["task_id"], None)
