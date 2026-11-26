"""``RunRef``: everything the plugin needs to turn a run's pixel coordinates back into map coordinates.

It travels through the service as ``client_ref = {"opaque": RunRef.to_dict()}`` (the service stores and
returns it untouched).  Run pixel coordinates: x right, y down, origin at the top-left corner of tile
(0, 0), in imagery pixels.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class RunRef:
    crs: str
    origin: Tuple[float, float]
    pixel_size: float
    rows: int
    cols: int
    tile_px: int = 512
    gsd_m: float = 0.5
    layer_name: str = ""
    layer_id: str = ""
    layer_source: str = ""
    area_wkt: str = ""
    area_km2: float = 0.0
    drawn_km2: float = 0.0
    classes_kept: List[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    gsd_y_m: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("layer_source", "layer_id", "layer_name"):
            d.pop(k, None)
        d["origin"] = list(self.origin)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunRef":
        d = dict(d or {})
        d["origin"] = tuple(d.get("origin") or (0.0, 0.0))
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    @classmethod
    def from_client_ref(cls, client_ref: Any) -> Optional["RunRef"]:
        if not isinstance(client_ref, dict):
            return None
        op = client_ref.get("opaque")
        return cls.from_dict(op) if isinstance(op, dict) and "origin" in op else None

    def to_client_ref(self) -> Dict[str, Any]:
        return {"opaque": self.to_dict()}

    def pixel_to_map(self, x: float, y: float) -> Tuple[float, float]:
        return (self.origin[0] + x * self.pixel_size, self.origin[1] - y * self.pixel_size)

    def map_to_pixel(self, mx: float, my: float) -> Tuple[float, float]:
        return ((mx - self.origin[0]) / self.pixel_size, (self.origin[1] - my) / self.pixel_size)

    def tile_side_map(self) -> float:
        return self.tile_px * self.pixel_size

    def tile_rect_map(self, row: int, col: int) -> Tuple[float, float, float, float]:
        """xmin, ymin, xmax, ymax of tile (row, col) in map units."""
        s = self.tile_side_map()
        x0 = self.origin[0] + col * s
        y1 = self.origin[1] - row * s
        return (x0, y1 - s, x0 + s, y1)

    def metres_per_pixel(self) -> float:
        return self.gsd_m

    def metres_per_pixel_xy(self) -> Tuple[float, float]:
        """East-west and north-south ground metres per pixel (equal unless the imagery is in degrees)."""
        return self.gsd_m, (self.gsd_y_m or self.gsd_m)
