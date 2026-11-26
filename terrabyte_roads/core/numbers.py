"""One way to write a count for people.

Every count the plugin shows (tiles, lines, the balance, hidden or deleted lines, in the panel, its dialogs, the recap,
the map labels and the message bar) goes through ``count``, so '1,190 tiles', '4,193 lines' and '13,810 left this
month' are written alike.  The panel's text is English and its other numbers (km, cm, km²) use a decimal point, so the
thousands are grouped the English way, whatever the system locale; this is the one place to change that.
"""
from __future__ import annotations

from typing import Any


def count(n: Any) -> str:
    """'7', '1,190', '13,810' (a whole number; anything else is written as given)."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def counted(n: Any, one: str, many: str) -> str:
    """'1 line', '4,193 lines'."""
    try:
        single = int(n) == 1
    except (TypeError, ValueError):
        single = False
    return f"{count(n)} {one if single else many}"
