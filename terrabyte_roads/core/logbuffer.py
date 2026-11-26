"""Ring buffer of the plugin's own log lines (for "Report a problem").  QgsMessageLog cannot be read back,
and on QGIS 4 its per-message Python signal is not delivered, so the plugin's ``log()`` helpers call
``record`` as well as writing to the QGIS message log."""
from __future__ import annotations

import collections

LOG_BUFFER: "collections.deque[str]" = collections.deque(maxlen=800)


def record(msg: str) -> None:
    msg = str(msg)
    if not LOG_BUFFER or LOG_BUFFER[-1] != msg:
        LOG_BUFFER.append(msg)
