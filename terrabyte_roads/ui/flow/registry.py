"""Registry of the flow's screens and dialogs, keyed by screen id (EX-Start, RV-Keep, ...).  Each flow module registers
its own; the controller routes ``go()``.

* ``screen(id)``  decorator: class(ctx) -> Screen shown in the dock's stack.
* ``dialog(id)``  decorator: function(ctx, **kw) -> QDialog, shown non-modally (``.open()``).
* ``alias(id)``   decorator: function(ctx, **kw) -> the real screen id (e.g. EX-Start picks the screen that fits).
"""
from __future__ import annotations

from typing import Callable, Dict

SCREENS: Dict[str, Callable] = {}
DIALOGS: Dict[str, Callable] = {}
ALIASES: Dict[str, Callable] = {}


def screen(board: str):
    def deco(cls):
        SCREENS[board] = cls
        cls.board = board
        return cls
    return deco


def dialog(board: str):
    def deco(fn):
        DIALOGS[board] = fn
        return fn
    return deco


def alias(board: str):
    def deco(fn):
        ALIASES[board] = fn
        return fn
    return deco


BARE_HEADER = {"EN-SignedOut", "EN-SignInWait", "EN-Update"}

DOCK_BOARDS = ["EN-SignedOut", "EN-SignInWait", "EN-Update", "EX-NoImagery", "EX-Start",
               "EX-DrawArea", "EX-Setup", "EX-OverBudget", "EX-Running", "EX-Failed",
               "RV-Keep", "RV-Export", "RV-Recap"]
DIALOG_BOARDS = ["EN-BeforeStart", "RV-Exit", "ST-Account", "ST-Billing"]
ALL_BOARDS = ["EN-Toolbar"] + DOCK_BOARDS + DIALOG_BOARDS


def load_lanes() -> None:
    """Import every flow module (entry, settings, extract, review) so it registers itself.  A module that is not
    there is skipped; any other import error is raised."""
    import importlib

    for name in ("entry", "settings", "extract", "review"):
        try:
            importlib.import_module(f"{__package__}.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"{__package__}.{name}":
                raise
