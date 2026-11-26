"""Analysis tools: processing algorithms, feature queries, line summaries."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from qgis.core import (
    NULL,
    Qgis,
    QgsApplication,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMapLayer,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDate, QDateTime, Qt, QTime

from ..context import BridgeContext, clamp_limit, crs_id, parse_bbox, parse_crs, require_str
from ..registry import CommandError, command

_LAYER_PARAM_TYPES = {"layer", "vector", "raster", "mesh", "pointcloud", "source", "annotation", "vectortile"}
_MULTI_PARAM_TYPES = {"multilayer"}
_LAYER_OUTPUT_TYPES = {"outputVector", "outputRaster", "outputLayer", "outputPointCloud", "outputVectorTile"}
_HELP_CAP = 1500


def _registry():
    return QgsApplication.processingRegistry()


_CODE_PROVIDER_IDS = {"script", "r"}


def _get_algorithm(algorithm_id: str):
    require_str("algorithm_id", algorithm_id)
    alg = _registry().algorithmById(algorithm_id)
    if alg is None:
        raise CommandError("not_found", f"unknown processing algorithm {algorithm_id!r} "
                                        "(use list_processing_algorithms to search)")
    return alg


@command("list_processing_algorithms")
def list_processing_algorithms(ctx: BridgeContext, search: Optional[str] = None,
                               provider: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    limit = clamp_limit(limit, 50, 200)
    needle = search.lower().strip() if isinstance(search, str) and search.strip() else None
    prov = provider.lower().strip() if isinstance(provider, str) and provider.strip() else None
    matches: List[Dict[str, Any]] = []
    for alg in _registry().algorithms():
        p = alg.provider()
        pid = p.id() if p else ""
        if prov and prov not in (pid.lower(), (p.name().lower() if p else "")):
            continue
        if needle:
            hay = " ".join([alg.id(), alg.displayName(), " ".join(alg.tags())]).lower()
            if needle not in hay:
                continue
        matches.append({"id": alg.id(), "name": alg.displayName(), "provider": pid, "group": alg.group()})
    matches.sort(key=lambda a: a["id"])
    return {"algorithms": matches[:limit], "total": len(matches), "truncated": len(matches) > limit}


def _plain(v: Any) -> Any:
    """Best-effort JSON-friendly rendering of a QGIS default value."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    try:
        if hasattr(v, "isValid") and not v.isValid():
            return None
        return str(v.toString()) if hasattr(v, "toString") else str(v)
    except Exception:
        return str(v)


@command("describe_processing_algorithm")
def describe_processing_algorithm(ctx: BridgeContext, algorithm_id: str) -> Dict[str, Any]:
    alg = _get_algorithm(algorithm_id)
    params = []
    for p in alg.parameterDefinitions():
        try:
            default = _plain(p.defaultValueForGui())
        except Exception:
            default = None
        params.append({
            "name": p.name(), "type": p.type(), "description": p.description(),
            "optional": bool(p.flags() & Qgis.ProcessingParameterFlag.Optional),
            "default": default,
        })
    outs = [{"name": o.name(), "type": o.type(), "description": o.description()}
            for o in alg.outputDefinitions()]
    help_text = alg.shortHelpString() or alg.shortDescription() or ""
    help_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", help_text)).strip()
    if len(help_text) > _HELP_CAP:
        help_text = help_text[:_HELP_CAP] + "..."
    return {"id": alg.id(), "name": alg.displayName(), "description": help_text,
            "parameters": params, "outputs": outs}


def _resolve_layer_value(ctx: BridgeContext, value: Any) -> Any:
    """A string naming a project layer (ID or unique name) becomes the layer; else unchanged
    (it may be a file path or a source URI)."""
    if isinstance(value, QgsMapLayer) or not isinstance(value, str) or not value:
        return value
    proj = ctx.project
    if (proj.mapLayer(value) is not None or proj.mapLayersByName(value)
            or any(l.name().lower() == value.lower() for l in proj.mapLayers().values())):
        return ctx.get_layer(value)
    return value


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.errors: List[str] = []

    def reportError(self, error, fatalError=False):
        self.errors.append(str(error))
        super().reportError(error, fatalError)


@command("run_processing_algorithm")
def run_processing_algorithm(ctx: BridgeContext, algorithm_id: str, parameters: Optional[dict] = None,
                             load_outputs: bool = True) -> Dict[str, Any]:
    template = _get_algorithm(algorithm_id)
    prov = template.provider()
    if prov is not None and prov.id().lower() in _CODE_PROVIDER_IDS:
        raise CommandError(
            "disabled",
            f"{algorithm_id} is a user script, so running it is arbitrary code execution, which is disabled.")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise CommandError("invalid_params", "parameters must be an object")
    alg = template.create()
    defs = {d.name(): d for d in alg.parameterDefinitions()}
    known = set(defs) | {o.name() for o in alg.outputDefinitions()}
    unknown = [k for k in parameters if k not in known]
    if unknown:
        raise CommandError("invalid_params", f"unknown parameter(s) {unknown}; valid: {sorted(defs)}")
    params: Dict[str, Any] = {}
    for k, v in parameters.items():
        d = defs.get(k)
        if d is not None and d.type() in _LAYER_PARAM_TYPES:
            v = _resolve_layer_value(ctx, v)
        elif d is not None and d.type() in _MULTI_PARAM_TYPES:
            v = [_resolve_layer_value(ctx, x) for x in v] if isinstance(v, (list, tuple)) \
                else _resolve_layer_value(ctx, v)
        params[k] = v

    pctx = QgsProcessingContext()
    pctx.setProject(ctx.project)
    pctx.setTransformContext(ctx.project.transformContext())
    feedback = _Feedback()
    ok, msg = alg.checkParameterValues(params, pctx)
    if not ok:
        raise CommandError("invalid_params", msg or "invalid parameters")
    try:
        results, ok = alg.run(params, pctx, feedback)
    except Exception as e:
        raise CommandError("invalid_params", f"{algorithm_id} failed: {e}") from None
    if not ok:
        detail = "; ".join(feedback.errors[:3]) or "algorithm reported failure"
        raise CommandError("invalid_params", f"{algorithm_id} failed: {detail}")

    out_defs = {o.name(): o for o in alg.outputDefinitions()}
    outputs: Dict[str, Any] = {}
    loaded: List[Dict[str, Any]] = []
    store = pctx.temporaryLayerStore()
    for name, value in results.items():
        od = out_defs.get(name)
        if od is not None and od.type() in _LAYER_OUTPUT_TYPES and isinstance(value, str) and value:
            tmp_layer = store.mapLayer(value)
            in_store = tmp_layer is not None
            layer = None
            if load_outputs:
                layer = store.takeMapLayer(tmp_layer) if in_store else _load_layer_from_path(value, od)
            if layer is not None:
                layer.setName(od.description() or name)
                ctx.project.addMapLayer(layer)
                loaded.append({"layer_id": layer.id(), "name": layer.name()})
                outputs[name] = {"layer_id": layer.id(), "name": layer.name()}
            elif in_store:
                outputs[name] = {"temporary_layer": value, "loaded": False,
                                 "note": "temporary layer is discarded unless load_outputs=true"}
            else:
                outputs[name] = value
        elif isinstance(value, (list, dict)):
            outputs[name] = value
        else:
            outputs[name] = _plain(value)
    return {"algorithm_id": alg.id(), "outputs": outputs, "loaded_layers": loaded}


def _load_layer_from_path(path: str, od) -> Optional[QgsMapLayer]:
    layer: QgsMapLayer
    if od.type() == "outputRaster":
        layer = QgsRasterLayer(path, path)
    else:
        layer = QgsVectorLayer(path, path, "ogr")
        if not layer.isValid():
            layer = QgsRasterLayer(path, path)
    return layer if layer.isValid() else None


_WKT_CAP = 2000


def _is_null(v: Any) -> bool:
    if v is None or v is NULL:
        return True
    if isinstance(v, (int, float, str, bool)):
        return False
    try:
        return bool(v == NULL)
    except Exception:
        return False


def _attr_safe(v: Any) -> Any:
    if _is_null(v):
        return None
    if isinstance(v, (QDateTime, QDate, QTime)):
        return v.toString(Qt.DateFormat.ISODate) if v.isValid() else None
    if isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray)) or type(v).__name__ == "QByteArray":
        return f"<{len(v)} bytes>"
    if isinstance(v, (list, tuple)):
        return [_attr_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _attr_safe(x) for k, x in v.items()}
    return str(v)


def _check_expression(expr_text: str, layer: QgsVectorLayer, what: str) -> QgsExpression:
    e = QgsExpression(expr_text)
    if e.hasParserError():
        raise CommandError("invalid_params", f"invalid {what} expression: {e.parserErrorString()}")
    ectx = QgsExpressionContext()
    ectx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    e.prepare(ectx)
    if e.hasEvalError():
        raise CommandError("invalid_params", f"invalid {what} expression: {e.evalErrorString()}")
    return e


@command("get_layer_features")
def get_layer_features(ctx: BridgeContext, layer_id: str, expression: Optional[str] = None,
                       fields: Optional[List[str]] = None, limit: int = 50,
                       include_geometry: str = "none", order_by: Optional[str] = None) -> Dict[str, Any]:
    layer = ctx.get_layer(layer_id, "vector")
    limit = clamp_limit(limit, 50, 500)
    if include_geometry not in ("none", "wkt", "centroid", "bbox"):
        raise CommandError("invalid_params", "include_geometry must be one of none, wkt, centroid, bbox")
    layer_fields = layer.fields()
    if fields is not None:
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            raise CommandError("invalid_params", "fields must be a list of field names")
        bad = [f for f in fields if layer_fields.lookupField(f) < 0]
        if bad:
            raise CommandError("invalid_params", f"unknown field(s) {bad}; available: "
                                                 f"{[f.name() for f in layer_fields][:50]}")
        names = list(fields)
    else:
        names = [f.name() for f in layer_fields][:100]

    base = QgsFeatureRequest()
    needs_geom = include_geometry != "none"
    if expression:
        needs_geom = _check_expression(expression, layer, "filter").needsGeometry() or needs_geom
        base.setFilterExpression(expression)

    if expression:
        cr = QgsFeatureRequest(base)
        if not needs_geom:
            cr.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        total = sum(1 for _ in layer.getFeatures(cr))
    else:
        total = layer.featureCount()

    req = QgsFeatureRequest(base)
    if order_by:
        m = re.match(r"^(.*?)(?:\s+(ASC|DESC))?\s*$", order_by.strip(), re.IGNORECASE | re.DOTALL)
        oexpr, direction = m.group(1), (m.group(2) or "ASC").upper()
        needs_geom = _check_expression(oexpr, layer, "order_by").needsGeometry() or needs_geom
        req.setOrderBy(QgsFeatureRequest.OrderBy([QgsFeatureRequest.OrderByClause(oexpr, direction == "ASC")]))
    if not needs_geom:
        req.setFlags(QgsFeatureRequest.Flag.NoGeometry)
    req.setLimit(limit)

    feats = []
    for f in layer.getFeatures(req):
        item: Dict[str, Any] = {"fid": f.id(), "attributes": {n: _attr_safe(f[n]) for n in names}}
        if include_geometry != "none":
            item["geometry"] = _geometry_summary(f.geometry(), include_geometry)
        feats.append(item)
    return {"layer_id": layer.id(), "crs": crs_id(layer.crs()), "features": feats, "total_matching": total,
            "returned": len(feats), "truncated": total > len(feats)}


def _geometry_summary(g: QgsGeometry, mode: str) -> Optional[Dict[str, Any]]:
    if g is None or g.isNull() or g.isEmpty():
        return None
    if mode == "wkt":
        wkt = g.asWkt(6)
        if len(wkt) > _WKT_CAP:
            return {"wkt": wkt[:_WKT_CAP], "truncated": True}
        return {"wkt": wkt, "truncated": False}
    if mode == "centroid":
        c = g.centroid().asPoint()
        return {"x": round(c.x(), 6), "y": round(c.y(), 6)}
    r = g.boundingBox()
    return {"xmin": round(r.xMinimum(), 6), "ymin": round(r.yMinimum(), 6),
            "xmax": round(r.xMaximum(), 6), "ymax": round(r.yMaximum(), 6)}


def _distance_area(layer: QgsVectorLayer) -> QgsDistanceArea:
    proj = QgsProject.instance()
    da = QgsDistanceArea()
    da.setSourceCrs(layer.crs(), proj.transformContext())
    ell = proj.ellipsoid()
    if not ell or ell == "NONE":
        ell = "EPSG:7030"
    da.setEllipsoid(ell)
    return da


def _to_layer_crs(geom: QgsGeometry, src_crs, layer_crs) -> QgsGeometry:
    g = QgsGeometry(geom)
    if src_crs != layer_crs:
        g = g.densifyByCount(20)
        g.transform(QgsCoordinateTransform(src_crs, layer_crs, QgsProject.instance()))
    return g


def _resolve_area(ctx: BridgeContext, area: Any, layer: QgsVectorLayer) -> Optional[QgsGeometry]:
    """Area spec -> clip polygon in the layer CRS."""
    if area is None:
        return None
    if not isinstance(area, dict) or "type" not in area:
        raise CommandError("invalid_params", "area must be an object with a 'type'")
    t = area["type"]
    if t == "bbox":
        rect = parse_bbox(area.get("bbox"))
        crs = parse_crs(area.get("crs"), "EPSG:4326")
        return _to_layer_crs(QgsGeometry.fromRect(rect), crs, layer.crs())
    if t == "map_view":
        return _to_layer_crs(QgsGeometry.fromRect(ctx.get_map_extent()), ctx.map_crs(), layer.crs())
    if t == "layer_extent":
        return QgsGeometry.fromRect(layer.extent())
    if t == "polygon_layer":
        pl = ctx.get_layer(area.get("layer_id"), "vector")
        if pl.geometryType() != Qgis.GeometryType.Polygon:
            raise CommandError("invalid_params", f"layer {pl.name()!r} is not a polygon layer")
        feats = pl.getSelectedFeatures() if area.get("selected_only") else pl.getFeatures()
        geoms = [f.geometry() for f in feats if not f.geometry().isNull()]
        if not geoms:
            raise CommandError("invalid_params", "polygon layer has no (selected) polygons")
        return _to_layer_crs(QgsGeometry.unaryUnion(geoms), pl.crs(), layer.crs())
    raise CommandError("invalid_params", f"unsupported area type {t!r}")


def _lines_only(g: QgsGeometry) -> QgsGeometry:
    """Linear parts of an intersection result (which may be a collection incl. points)."""
    if g.type() == Qgis.GeometryType.Line:
        return g
    lines = [QgsGeometry(p.clone()) for p in g.constGet().parts()] if g.isMultipart() else []
    lines = [p for p in lines if p.type() == Qgis.GeometryType.Line]
    return QgsGeometry.collectGeometry(lines) if lines else QgsGeometry()


@command("summarise_lines")
def summarise_lines(ctx: BridgeContext, layer_id: str, class_field: Optional[str] = "class",
                    area: Optional[dict] = None) -> Dict[str, Any]:
    layer = ctx.get_layer(layer_id, "vector")
    if layer.geometryType() != Qgis.GeometryType.Line:
        raise CommandError("invalid_params", f"layer {layer.name()!r} is not a line layer")
    clip = _resolve_area(ctx, area, layer)
    da = _distance_area(layer)
    use_field = bool(class_field) and layer.fields().lookupField(class_field) >= 0
    req = QgsFeatureRequest()
    if clip is not None:
        req.setFilterRect(clip.boundingBox())
    lengths: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    total = 0.0
    n = 0
    for f in layer.getFeatures(req):
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        if clip is not None:
            g = g.intersection(clip)
            if g is None or g.isNull() or g.isEmpty():
                continue
            g = _lines_only(g)
            if g.isNull() or g.isEmpty():
                continue
        length = da.measureLength(g)
        v = f[class_field] if use_field else "all"
        key = "(null)" if _is_null(v) else str(v)
        lengths[key] = lengths.get(key, 0.0) + length
        counts[key] = counts.get(key, 0) + 1
        total += length
        n += 1
    by_class = [{"class": k, "length_m": round(v, 2), "count": counts[k],
                 "share_pct": round(100.0 * v / total, 2) if total > 0 else 0.0}
                for k, v in sorted(lengths.items(), key=lambda kv: -kv[1])]
    res: Dict[str, Any] = {"layer_id": layer.id(), "feature_count": n, "total_length_m": round(total, 2),
                           "by_class": by_class[:100]}
    if len(by_class) > 100:
        res["by_class_truncated"] = True
    if clip is not None:
        km2 = da.measureArea(clip) / 1e6
        res["area_km2"] = round(km2, 6)
        res["density_km_per_km2"] = round((total / 1000.0) / km2, 4) if km2 > 0 else None
    return res
