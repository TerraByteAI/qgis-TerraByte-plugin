"""Project and map-extent tools."""
from __future__ import annotations

from typing import Any, Dict, Optional

from qgis.core import Qgis, QgsRectangle

from ...core.version import plugin_version
from ..context import BridgeContext, crs_id, extent_dict, parse_crs
from ..registry import CommandError, command


def _extent(ctx: BridgeContext) -> Dict[str, Any]:
    return extent_dict(ctx.get_map_extent(), ctx.map_crs())


@command("get_project_info")
def get_project_info(ctx: BridgeContext) -> Dict[str, Any]:
    p = ctx.project
    return {
        "title": p.title() or p.baseName() or "",
        "file_path": p.fileName() or None,
        "crs": crs_id(ctx.map_crs()),
        "layer_count": len(p.mapLayers()),
        "map_extent": _extent(ctx),
        "qgis_version": Qgis.version(),
        "plugin_version": plugin_version(),
    }


@command("get_map_extent")
def get_map_extent(ctx: BridgeContext) -> Dict[str, Any]:
    scale: Optional[float] = ctx.canvas.scale() if ctx.canvas is not None else None
    return {"map_extent": _extent(ctx), "scale": scale}


@command("set_map_extent")
def set_map_extent(ctx: BridgeContext, xmin: float, ymin: float, xmax: float, ymax: float,
                   crs: str = "EPSG:4326") -> Dict[str, Any]:
    try:
        vals = [float(v) for v in (xmin, ymin, xmax, ymax)]
    except (TypeError, ValueError):
        raise CommandError("invalid_params", "xmin/ymin/xmax/ymax must be numbers") from None
    if not (vals[0] < vals[2] and vals[1] < vals[3]):
        raise CommandError("invalid_params", "need xmin < xmax and ymin < ymax")
    ctx.set_map_extent(QgsRectangle(*vals), parse_crs(crs))
    return {"map_extent": _extent(ctx)}


@command("save_project")
def save_project(ctx: BridgeContext, path: Optional[str] = None) -> Dict[str, Any]:
    p = ctx.project
    if path:
        if not isinstance(path, str):
            raise CommandError("invalid_params", "path must be a string")
        target = path if path.lower().endswith((".qgz", ".qgs")) else path + ".qgz"
    else:
        target = p.fileName()
        if not target:
            raise CommandError("invalid_params", "project has no file yet; pass path (e.g. /home/me/roads.qgz)")
    if not p.write(target):
        raise CommandError("unavailable", f"could not write project to {target}")
    return {"file_path": p.fileName() or target}
