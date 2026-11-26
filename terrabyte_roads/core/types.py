"""Core data types shared across the plugin.

This module must not import QGIS or GDAL (pure Python + NumPy), so it can be
imported anywhere. Plugin code must stay Python 3.9 compatible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

ProgressCallback = Callable[[float, str], None]
CancelCheck = Callable[[], bool]


class TaskCancelled(Exception):
    """Raised inside worker code when the user/MCP client cancelled the task."""


@dataclass
class Polyline:
    """A line with a class id and a confidence in [0, 1].

    ``coords`` is a float64 array of shape (N, 2), N >= 2, holding (x, y) pairs, either:

    * continuous pixel coordinates of a tile: (0, 0) is the top-left corner of the
      top-left pixel, x grows to the right (columns) and y grows downward (rows).
      The centre of pixel (row r, col c) is (c + 0.5, r + 0.5); or
    * map coordinates (x = easting, y = northing) in the working CRS, after
      georeferencing.
    """

    coords: np.ndarray
    class_id: int = 0
    confidence: float = 1.0

    def __post_init__(self) -> None:
        self.coords = np.asarray(self.coords, dtype=np.float64).reshape(-1, 2)
        self.class_id = int(self.class_id)
        self.confidence = float(self.confidence)

    @property
    def length(self) -> float:
        """Planar length in coordinate units."""
        if len(self.coords) < 2:
            return 0.0
        d = np.diff(self.coords, axis=0)
        return float(np.hypot(d[:, 0], d[:, 1]).sum())

    def copy(self, coords: Optional[np.ndarray] = None) -> "Polyline":
        return Polyline(self.coords.copy() if coords is None else coords, self.class_id, self.confidence)
