"""Command registry and dispatch for the bridge.

Handlers are plain functions registered with ``@command``. Parameters are bound by
signature, so an unknown or misspelled parameter is rejected instead of silently
ignored (pattern borrowed from qgis-mcp)::

    from ..registry import command, CommandError

    @command("list_layers")
    def list_layers(ctx, type_filter: str = "all", limit: int = 200) -> dict:
        ...
        return {"layers": [...], "total": n}

Rules for handlers:
* run on the QGIS main thread; must return quickly (< ~5 s). Anything slower must
  start a ``ManagedTask`` (core/tasks.py) and return ``{"task_id": ..., "status": "queued"}``;
* return a JSON-serialisable dict (no NaN/inf - they are replaced with null);
* raise ``CommandError(code, message)`` for expected failures (``ERROR_CODES``); any other
  exception is reported as ``internal`` with the exception text;
* keep results compact: summaries and IDs, cap lists, never dump full geometries.
"""
from __future__ import annotations

import importlib
import inspect
import math
import pkgutil
import sys
import traceback
from typing import Any, Callable, Dict, List

ERROR_CODES = (
    "unauthorized",
    "unknown_command",
    "invalid_params",
    "not_found",
    "disabled",
    "unavailable",
    "busy",
    "internal",
)

Handler = Callable[..., Dict[str, Any]]
_COMMANDS: Dict[str, Handler] = {}
_LOADED: set = set()
_LOAD_ERRORS: Dict[str, str] = {}


class CommandError(Exception):
    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            code = "internal"
        super().__init__(message)
        self.code = code
        self.message = message


def command(name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        old = _COMMANDS.get(name)
        if old is not None and old is not fn and not (
                getattr(old, "__module__", None) == fn.__module__
                and getattr(old, "__qualname__", None) == fn.__qualname__):
            raise ValueError(f"duplicate bridge command {name!r}")
        _COMMANDS[name] = fn
        return fn

    return deco


def load_handlers() -> List[str]:
    """Import every module in bridge/handlers.  Idempotent and self-healing.

    After an in-place plugin upgrade or reload this registry module can be a fresh, empty one while the handler
    modules are still in ``sys.modules`` (so a plain import would run no decorator and leave ``_COMMANDS`` empty,
    and the handlers would stay bound to the old registry's ``CommandError``).  A handler module that is already
    imported but not yet registered here is therefore reloaded.  A module that fails to import does not hide the
    others: its real exception is kept in ``load_errors()`` for the caller to show.  Returns command names."""
    from . import handlers

    for mod in pkgutil.iter_modules(handlers.__path__):
        if mod.name.startswith("_"):
            continue
        full = f"{handlers.__name__}.{mod.name}"
        if full in _LOADED:
            continue
        try:
            existing = sys.modules.get(full)
            if existing is None:
                importlib.import_module(full)
            else:
                importlib.reload(existing)
            _LOADED.add(full)
            _LOAD_ERRORS.pop(full, None)
        except Exception as e:
            _LOAD_ERRORS[full] = f"{type(e).__name__}: {e}"
            _LOAD_ERRORS[full + "#traceback"] = traceback.format_exc()
    return sorted(_COMMANDS)


def load_errors() -> Dict[str, str]:
    """Handler modules that could not be imported -> ``ExceptionType: message`` (no tracebacks)."""
    return {k: v for k, v in _LOAD_ERRORS.items() if not k.endswith("#traceback")}


def command_names() -> List[str]:
    return sorted(_COMMANDS)


def dispatch(name: str, params: Any, ctx: Any) -> Dict[str, Any]:
    fn = _COMMANDS.get(name)
    if fn is None:
        raise CommandError("unknown_command", f"unknown command {name!r}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise CommandError("invalid_params", "params must be a JSON object")
    try:
        bound = inspect.signature(fn).bind(ctx, **params)
    except TypeError as e:
        raise CommandError("invalid_params", f"{name}: {e}") from None
    result = fn(*bound.args, **bound.kwargs)
    if result is None:
        result = {}
    if not isinstance(result, dict):
        raise CommandError("internal", f"{name} returned {type(result).__name__}, expected dict")
    return json_safe(result)


def json_safe(obj: Any) -> Any:
    """Replace NaN/inf with None and coerce tuples/sets/numpy scalars for JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return json_safe(item())
        except (TypeError, ValueError):
            pass
    return str(obj)
