"""Generic task tools: get_task_status, cancel_task, list_tasks."""
from __future__ import annotations

from typing import Any, Dict

from ..context import BridgeContext, clamp_limit, require_str
from ..registry import CommandError, command


@command("get_task_status")
def get_task_status(ctx: BridgeContext, task_id: str) -> Dict[str, Any]:
    require_str("task_id", task_id)
    rec = ctx.tasks.status(task_id)
    if rec is None:
        raise CommandError("not_found", f"no task {task_id!r} (tasks are forgotten when QGIS restarts)")
    return rec


@command("cancel_task")
def cancel_task(ctx: BridgeContext, task_id: str) -> Dict[str, Any]:
    require_str("task_id", task_id)
    rec = ctx.tasks.status(task_id)
    if rec is None:
        raise CommandError("not_found", f"no task {task_id!r}")
    cancelled = ctx.tasks.cancel(task_id)
    return {"task_id": task_id, "cancel_requested": cancelled, "status": ctx.tasks.status(task_id)["status"]}


@command("list_tasks")
def list_tasks(ctx: BridgeContext, limit: int = 20) -> Dict[str, Any]:
    return {"tasks": ctx.tasks.list(clamp_limit(limit, 20, 100))}
