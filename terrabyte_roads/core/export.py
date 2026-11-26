"""Write review lines to GeoPackage, GeoJSON or Shapefile (OGR, QGIS-free).

Fields written: class, class_id, length_m.  An export never replaces anything: ``write_lines`` refuses to touch
an existing file, and ``unique_target`` picks a free name (``Roads 2026-11-18 14-32 (2)``) when the name is taken
by a file in the folder (the data file, its parts or its ``.qml`` style) or by a layer already in the project.

A write that fails raises ``ExportError``, whose ``user_text`` is one plain sentence naming the folder and what to
do; the driver's own words (``sqlite3_open(...) failed: unable to open database file``) stay in ``detail``, for the
QGIS message log only.  The folder is checked before anything is written, by creating, writing and removing a small
temp file there (``check_folder``), not by parsing what GDAL says.
"""
from __future__ import annotations

import errno
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from osgeo import ogr, osr

FORMATS = {"GeoPackage": ("GPKG", ".gpkg"), "GeoJSON": ("GeoJSON", ".geojson"),
           "Shapefile": ("ESRI Shapefile", ".shp")}
MIN_LINE_M = 0.05
GEOJSON_EPSG = 4326
FIELDS = (("class", ogr.OFTString), ("class_id", ogr.OFTInteger), ("length_m", ogr.OFTReal))

MISSING, NOT_WRITABLE, IN_USE, DISK_FULL, TOO_LONG, UNKNOWN = (
    "missing", "not_writable", "in_use", "disk_full", "too_long", "unknown")
WIN_MAX_PATH = 260
PROBE_BYTES = 8192
LOW_SPACE_BYTES = 1 << 20
JOURNAL = "-journal"


class ExportError(RuntimeError):
    """A write the export could not make.  ``kind`` is one of the constants above, ``folder`` the folder it wrote
    to, ``user_text`` the sentence the screen shows and ``detail`` the raw text (message log only)."""

    def __init__(self, kind: str, folder: "str | Path", detail: str = ""):
        super().__init__(detail or kind)
        self.kind = kind
        self.folder = str(folder or "")
        self.detail = detail

    @property
    def user_text(self) -> str:
        return user_text(self.kind, self.folder)


def shown_folder(folder: "str | Path") -> str:
    """The folder as the user's system spells it (``C:\\Program Files``, never ``C:/Program Files``)."""
    s = str(folder or "").strip()
    return os.path.normpath(s) if s else ""


def user_text(kind: str, folder: "str | Path") -> str:
    """One short sentence for the export card: what went wrong with which folder, and what to do."""
    f = shown_folder(folder) or "this folder"
    return {
        MISSING: f"Can't find the folder {f}. Choose another folder.",
        NOT_WRITABLE: f"Can't save to {f}. Choose a folder you can write to.",
        IN_USE: f"A file in {f} is open in another program. Close it, or choose another folder.",
        DISK_FULL: f"The disk with {f} is full. Free some space, or choose another folder.",
        TOO_LONG: f"The path to {f} is too long. Choose a folder with a shorter path, or a shorter layer name.",
    }.get(kind, f"Couldn't save to {f}. Try again, or choose another folder.")


def os_error_kind(e: OSError, path: "str | Path") -> str:
    """The kind of an ``OSError`` from creating or writing ``path``.  Windows codes first: a file open in another
    program (32, 33) arrives as a PermissionError with the same errno as "access denied" (5)."""
    w = getattr(e, "winerror", None)
    if w in (32, 33):
        return IN_USE
    if w in (39, 112):
        return DISK_FULL
    if w == 206:
        return TOO_LONG
    if w in (5, 19):
        return NOT_WRITABLE
    n = e.errno
    if n in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return DISK_FULL
    if n == errno.ENAMETOOLONG:
        return TOO_LONG
    if n in (errno.EACCES, errno.EPERM, errno.EROFS):
        return NOT_WRITABLE
    if n in (errno.EBUSY, getattr(errno, "ETXTBSY", -1)):
        return IN_USE
    if n in (errno.ENOENT, errno.ENOTDIR) or w in (2, 3, 267):
        if not Path(path).parent.is_dir():
            return MISSING
        if _too_long_for_windows(path):
            return TOO_LONG
    return UNKNOWN


def _too_long_for_windows(path: "str | Path") -> bool:
    return os.name == "nt" and len(str(path)) + len(JOURNAL) >= WIN_MAX_PATH


def _probe_name(name: str) -> str:
    """A free file name as long as the longest one the export makes beside ``name`` (its journal file), so a path that
    is too long fails here and not half-way through the write."""
    tag = f".~rex-{secrets.token_hex(4)}"
    n = max(len(name) + len(JOURNAL), len(tag) + 4)
    return tag + "_" * (n - len(tag) - 4) + ".tmp"


def _remove_quietly(p: Path, tries: int = 3) -> None:
    for i in range(tries):
        try:
            if p.exists():
                p.unlink()
            return
        except OSError:
            time.sleep(0.05 * (i + 1))


def check_folder(folder: "str | Path", name: str = "") -> None:
    """Raise ``ExportError`` when a file called ``name`` can't be written in ``folder``: the folder is missing, can't
    be written to, the disk is full or the path is too long.  Checked by creating, writing and removing a small temp
    file there (permissions alone can't say: Windows folder rights are not what ``os.access`` reads).

    It fails open: an error it can't name (a network or virtual drive that refuses something the export itself never
    asks for) does not block the export; the real write decides, and ``explain`` names its failure."""
    f = Path(folder) if str(folder or "").strip() else None
    if f is None or not f.is_dir():
        raise ExportError(MISSING, folder, f"not an existing folder: {folder!s}")
    probe = f / _probe_name(name or "Roads.gpkg")
    fd = None
    kind, why = UNKNOWN, ""
    try:
        try:
            fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
            os.write(fd, b"\0" * PROBE_BYTES)
        except OSError as e:
            kind, why = os_error_kind(e, probe), str(e)
        else:
            for step in (os.fsync, os.close):
                try:
                    step(fd)
                except OSError as e:
                    if os_error_kind(e, probe) == DISK_FULL:
                        kind, why = DISK_FULL, str(e)
                if step is os.close:
                    fd = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _remove_quietly(probe)
    if kind != UNKNOWN:
        raise ExportError(kind, folder, f"folder check {probe}: {why}")


def explain(exc: BaseException, path: "str | Path") -> ExportError:
    """An ``ExportError`` for a write of ``path`` that failed with ``exc``.  The OS error when there is one; else what a
    fresh check of the folder finds (it may have gone, or become full, while writing); else a few words of the driver's
    text as a last hint; else ``UNKNOWN``.  The raw text is kept in ``detail`` only."""
    if isinstance(exc, ExportError):
        return exc
    path = Path(path)
    folder = path.parent
    detail = f"{type(exc).__name__}: {exc}"
    kind = os_error_kind(exc, path) if isinstance(exc, OSError) else UNKNOWN
    if kind == UNKNOWN:
        try:
            check_folder(folder, path.name)
        except ExportError as e2:
            kind, detail = e2.kind, f"{detail} | {e2.detail}"
    if kind == UNKNOWN and _low_space(folder):
        kind = DISK_FULL
    if kind == UNKNOWN and _too_long_for_windows(path):
        kind = TOO_LONG
    if kind == UNKNOWN:
        kind = _hint(str(exc))
    return ExportError(kind, folder, detail)


def _low_space(folder: Path) -> bool:
    try:
        return shutil.disk_usage(str(folder)).free < LOW_SPACE_BYTES
    except OSError:
        return False


_HINTS = ((DISK_FULL, ("disk is full", "disk full", "no space left", "not enough space")),
          (IN_USE, ("used by another process", "database is locked", "sharing violation", "lock violation")),
          (NOT_WRITABLE, ("permission denied", "access is denied", "readonly database", "read-only file system")),
          (TOO_LONG, ("name too long", "filename or extension is too long")))


def _hint(text: str) -> str:
    t = (text or "").lower()
    for kind, words in _HINTS:
        if any(w in t for w in words):
            return kind
    return UNKNOWN


def output_crs_label(fmt: str, crs_authid: str = "") -> str:
    """The coordinate system a file of this format is written in (``EPSG:4326`` for GeoJSON, else the imagery's)."""
    return f"EPSG:{GEOJSON_EPSG}" if fmt == "GeoJSON" else (crs_authid or "")


def extension(fmt: str) -> str:
    return FORMATS[fmt][1]


def default_layer_name(now: Optional[float] = None) -> str:
    """``Roads YYYY-MM-DD HH-MM`` in local time (no colon: it is also the file name)."""
    return "Roads " + time.strftime("%Y-%m-%d %H-%M", time.localtime(now))


def file_stem(name: str) -> str:
    """The layer name as a file name: only characters no file system accepts are replaced, spaces stay."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (name or "").strip()).strip(" .")
    return s[:100] or "roads"


def is_hidden_path(path: "str | Path") -> bool:
    """True when any folder in ``path`` below the user's home starts with a dot (``~/.local/share/...``)."""
    p = Path(path).expanduser()
    try:
        rel = p.resolve().relative_to(Path.home().resolve())
        parts = rel.parts
    except (ValueError, OSError):
        parts = p.parts
    return any(x.startswith(".") and x not in (".", "..") for x in parts)


def documents_folder() -> Path:
    """The user's Documents folder (home when it has none)."""
    try:
        from qgis.PyQt.QtCore import QStandardPaths

        d = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        if d and Path(d).is_dir():
            return Path(d)
    except Exception:
        pass
    d2 = Path.home() / "Documents"
    return d2 if d2.is_dir() else Path.home()


def default_folder(project_file: str = "") -> Path:
    """Where an export goes unless the user picks another folder: the project's folder when the project is saved,
    otherwise the Documents folder.  Never a hidden profile folder (those fall back to Documents)."""
    if project_file:
        base = Path(project_file).parent
        if not is_hidden_path(base):
            return base
    return documents_folder()


def target_path(folder: "str | Path", name: str, fmt: str) -> Path:
    return Path(folder) / f"{file_stem(name)}{extension(fmt)}"


def _sidecars(path: Path) -> Iterable[Path]:
    if path.suffix.lower() == ".shp":
        return [path.with_suffix(e) for e in (".shp", ".shx", ".dbf", ".prj", ".cpg")]
    return [path]


def style_file(path: "str | Path") -> Optional[Path]:
    """The ``.qml`` the export writes beside a GeoJSON or a Shapefile (a GeoPackage keeps its style inside)."""
    p = Path(path)
    return None if p.suffix.lower() == ".gpkg" else p.with_suffix(".qml")


def exists_on_disk(path: "str | Path") -> bool:
    """True when the data file, any of its parts, or the ``.qml`` it would get is there already (an old style
    file, perhaps read-only or open elsewhere, is never written over either)."""
    qml = style_file(path)
    return any(p.exists() for p in _sidecars(Path(path))) or (qml is not None and qml.exists())


def unique_target(folder: "str | Path", name: str, fmt: str,
                  taken_layer_names: Iterable[str] = ()) -> Tuple[str, Path]:
    """``(name, path)`` that collides with no existing file in ``folder`` and no name in ``taken_layer_names``.
    The name is kept when it is free, else `` (2)``, `` (3)`` ... is added."""
    name = (name or "").strip() or default_layer_name()
    taken = {t for t in taken_layer_names}
    n, cand = 1, name
    while True:
        path = target_path(folder, cand, fmt)
        if cand not in taken and not exists_on_disk(path):
            return cand, path
        n += 1
        cand = f"{name} ({n})"


def relative_path(layer_name: str, fmt: str) -> str:
    """Just the file name, as it will be written."""
    return f"{file_stem(layer_name)}{extension(fmt)}"


def write_lines(path: "str | Path", fmt: str, lines: Sequence[Dict[str, Any]], crs_wkt: str,
                layer_name: str) -> Dict[str, Any]:
    """``lines``: dicts with ``coords`` (map coordinates), ``class_id``, ``class_name``,
    ``length_m`` (true ground metres, written as given).  GeoJSON is reprojected to EPSG:4326 (longitude,
    latitude); GeoPackage and Shapefile keep ``crs_wkt``.  Lines shorter than ``MIN_LINE_M`` are skipped.
    Returns {path, layer_name, feature_count, format, crs}.

    Raises ``ExportError`` when the folder is missing (it is never created), can't be written to, is full or the path
    is too long, checked before anything is written, and for any failure of the write itself; nothing it started is
    left behind.  ``FileExistsError`` when the file is there already."""
    driver, _ext = FORMATS[fmt]
    path = Path(path)
    check_folder(path.parent, path.name)
    drv = ogr.GetDriverByName(driver)
    srs = osr.SpatialReference()
    if crs_wkt:
        srs.ImportFromWkt(crs_wkt)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = None
    if crs_wkt and fmt == "GeoJSON":
        src = srs
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(GEOJSON_EPSG)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if not src.IsSame(srs):
            tr = osr.CoordinateTransformation(src, srs)
    lines = [ln for ln in lines if float(ln["length_m"]) >= MIN_LINE_M]
    if exists_on_disk(path):
        raise FileExistsError(f"{path} already exists")
    try:
        return _write(drv, driver, path, fmt, lines, srs if crs_wkt else None, tr, layer_name, bool(crs_wkt))
    except Exception as e:
        _remove_partial(path)
        raise explain(e, path) from e
    except BaseException:
        _remove_partial(path)
        raise


def _remove_partial(path: Path) -> None:
    """Delete what a failed ``write_lines`` created (the file, its sidecars, a GeoPackage's journal files); nothing
    else can be at this path, because ``write_lines`` never starts on an existing one."""
    extra = [Path(str(path) + s) for s in ("-journal", "-wal", "-shm")] if path.suffix.lower() == ".gpkg" else []
    for p in list(_sidecars(path)) + extra:
        _remove_quietly(p)


def _write(drv, driver: str, path: Path, fmt: str, lines, srs, tr, layer_name: str, has_crs: bool) -> Dict[str, Any]:
    ds = drv.CreateDataSource(str(path))
    if ds is None:
        raise RuntimeError(f"cannot create {path}")
    lyr = None
    try:
        opts = ["SPATIAL_INDEX=YES"] if driver == "GPKG" else []
        lyr = ds.CreateLayer(layer_name if driver == "GPKG" else path.stem, srs, ogr.wkbLineString, opts)
        if lyr is None:
            raise RuntimeError(f"cannot create layer {layer_name!r} in {path}")
        for name, typ in FIELDS:
            lyr.CreateField(ogr.FieldDefn(name, typ))
        defn = lyr.GetLayerDefn()
        lyr.StartTransaction()
        for ln in lines:
            f = ogr.Feature(defn)
            g = ogr.Geometry(ogr.wkbLineString)
            for x, y in ln["coords"]:
                if tr is not None:
                    x, y = tr.TransformPoint(float(x), float(y))[:2]
                g.AddPoint_2D(float(x), float(y))
            f.SetGeometry(g)
            f.SetField("class", ln["class_name"])
            f.SetField("class_id", int(ln["class_id"]))
            f.SetField("length_m", float(ln["length_m"]))
            lyr.CreateFeature(f)
        lyr.CommitTransaction()
    finally:
        lyr = None
        ds = None
    return {"path": str(path), "layer_name": layer_name if driver == "GPKG" else path.stem,
            "feature_count": len(lines), "format": fmt,
            "crs": (f"EPSG:{GEOJSON_EPSG}" if fmt == "GeoJSON" else "") if has_crs else ""}
