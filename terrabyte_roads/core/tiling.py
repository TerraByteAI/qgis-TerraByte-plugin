"""Tiling of a raster into overlapping square windows with an exact partition of "core" regions.

Pure Python + NumPy. Tile origins are multiples of ``stride = tile_size - overlap``; tiles that
hang over the raster edge are read with zero padding. Each tile *owns* a core box; core boxes
partition the raster exactly: an overlap between neighbours is split in half, outer edges belong
to the edge tiles.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Tuple

import numpy as np


@dataclass(frozen=True)
class TileWindow:
    row_off: int
    col_off: int
    size: int
    core: Tuple[int, int, int, int]

    @property
    def core_rect_xy(self) -> Tuple[float, float, float, float]:
        """Core as (xmin, ymin, xmax, ymax) in raster pixel coordinates (x = column)."""
        r0, c0, r1, c1 = self.core
        return float(c0), float(r0), float(c1), float(r1)


def _axis(length: int, tile: int, overlap: int) -> List[Tuple[int, int, int]]:
    """(origin, core_start, core_end) per tile along one axis."""
    stride = tile - overlap
    n = 1 if length <= tile else -(-(length - tile) // stride) + 1
    half = overlap // 2
    out = []
    for i in range(n):
        o = i * stride
        start = 0 if i == 0 else o + half
        end = length if i == n - 1 else o + stride + half
        out.append((o, start, end))
    return out


def _check(tile_size: int, overlap: int) -> None:
    if tile_size < 1 or overlap < 0 or overlap * 2 >= tile_size:
        raise ValueError("need tile_size >= 1 and 0 <= overlap < tile_size / 2")


def make_tiles(height: int, width: int, tile_size: int, overlap: int) -> List[TileWindow]:
    """Row-major list of tiles covering the raster; cores partition it exactly."""
    if height < 1 or width < 1:
        raise ValueError("raster must have at least one pixel")
    _check(tile_size, overlap)
    rows = _axis(height, tile_size, overlap)
    cols = _axis(width, tile_size, overlap)
    return [TileWindow(ro, co, tile_size, (r0, c0, r1, c1))
            for ro, r0, r1 in rows for co, c0, c1 in cols]


def count_tiles(height: int, width: int, tile_size: int, overlap: int) -> int:
    _check(tile_size, overlap)
    stride = tile_size - overlap

    def n(length: int) -> int:
        return 1 if length <= tile_size else -(-(length - tile_size) // stride) + 1

    return n(max(1, height)) * n(max(1, width))


def read_tile(src: Any, win: TileWindow, dtype=np.float32) -> np.ndarray:
    """Read a (C, size, size) tile, zero-padded beyond the raster.

    ``src`` is a NumPy array (C, H, W) or a GDAL dataset (anything with ``RasterXSize``,
    ``RasterYSize``, ``RasterCount`` and ``ReadAsArray(xoff, yoff, xsize, ysize)``).
    """
    if isinstance(src, np.ndarray):
        c, h, w = src.shape
    else:
        c, h, w = src.RasterCount, src.RasterYSize, src.RasterXSize
    out = np.zeros((c, win.size, win.size), dtype=dtype)
    r1, c1 = min(win.row_off + win.size, h), min(win.col_off + win.size, w)
    if r1 <= win.row_off or c1 <= win.col_off:
        return out
    if isinstance(src, np.ndarray):
        data = src[:, win.row_off:r1, win.col_off:c1]
    else:
        data = src.ReadAsArray(win.col_off, win.row_off, c1 - win.col_off, r1 - win.row_off)
        data = np.asarray(data).reshape(c, r1 - win.row_off, c1 - win.col_off)
    out[:, : r1 - win.row_off, : c1 - win.col_off] = data
    return out
