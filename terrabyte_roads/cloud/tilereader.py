"""Reads the tiles of a run off the interface thread.

``TileReader`` is a Python thread that reads the tiles of one batch, in order, at most ``READ_AHEAD`` ahead of the
upload: the PNG each upload sends (``grid.tile_png_steps``) or, with ``check``, whether the tile holds imagery
(``grid.tile_rgb_steps`` and ``grid.tile_has_imagery``).  Rotated rasters and every resolution go through the same
reader.  The main thread (``runner.RunController``) takes ``("tile", row, col, value)`` items from ``out`` and calls
``taken`` for each to make room for the next; an ``("error", ...)`` item reports a failure and ``("end", ...)``
always comes last.

Why it is safe beside QGIS:
* every tile is read through its own GDAL dataset handle, opened and used only in this thread (a GDAL dataset
  handle must not be shared between threads);
* it uses GDAL, numpy and zlib only and touches no Qt or QGIS object: this module imports no Qt and no QGIS, and
  the grid functions it calls import only GDAL;
* GDAL and numpy release Python's lock while they work, so the interface thread keeps running;
* ``stop`` is checked between the pieces of a tile (``grid.STEP_SAMPLES``), so the thread ends within one piece.
  Stop does not wait for it; a run that is let go (a project change, the raster removed, QGIS quitting,
  ``dispose``) waits for it to leave its piece, at most ``runner.READER_JOIN_S``, so the file is closed when that
  returns.
"""
from __future__ import annotations

import copy
import queue
import threading
import time

from .grid import tile_has_imagery, tile_png_steps, tile_rgb_steps

READ_AHEAD = 8


class TileReader(threading.Thread):
    """Reads the tiles of one batch off the interface thread (see the module notes)."""

    def __init__(self, source: str, area, tiles, check: bool = False):
        super().__init__(name="RoadExtractor-tiles", daemon=True)
        self.source, self.area, self.tiles, self.check = source, copy.copy(area), list(tiles), check
        self.out: "queue.Queue" = queue.Queue()
        self.halt = threading.Event()
        self.room = threading.Semaphore(READ_AHEAD)
        self.read_s = 0.0
        self.read_n = 0

    def stop(self) -> None:
        self.halt.set()

    def taken(self) -> None:
        self.room.release()

    def run(self) -> None:
        try:
            for r, c in self.tiles:
                while not self.room.acquire(timeout=0.05):
                    if self.halt.is_set():
                        return
                if self.halt.is_set():
                    return
                t0 = time.perf_counter()
                steps = (tile_rgb_steps if self.check else tile_png_steps)(self.source, self.area, r, c)
                while True:
                    if self.halt.is_set():
                        return
                    try:
                        next(steps)
                    except StopIteration as done:
                        value = done.value
                        break
                self.read_s += time.perf_counter() - t0
                self.read_n += 1
                self.out.put(("tile", r, c, tile_has_imagery(value) if self.check else value))
        except Exception as e:
            self.out.put(("error", None, None, e))
        finally:
            self.out.put(("end", None, None, None))
