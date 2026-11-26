"""TerraByte Road Extractor - QGIS plugin entry point."""
import os
import sys

KEEP_MODULES_ENV = "TERRABYTE_ROADS_KEEP_MODULES"


def purge_modules(include_root: bool = False) -> int:
    """Drop this package's modules from ``sys.modules`` so the next import reads the files again; returns how many.

    QGIS can unload and reload the plugin, or the user can install a new ZIP over a loaded one, without restarting
    the process.  Modules cached from the old copy would otherwise be mixed with the new files.  Called when the plugin
    unloads and, for an upgrade over an old copy that did not clean up, when the new copy is loaded."""
    import importlib

    pkg = __name__
    names = [m for m in list(sys.modules) if m.startswith(pkg + ".")]
    if include_root:
        names.append(pkg)
    for m in names:
        sys.modules.pop(m, None)
    importlib.invalidate_caches()
    return len(names)


def classFactory(iface):
    if not os.environ.get(KEEP_MODULES_ENV):
        purge_modules()
    try:
        from . import integrity

        bad = integrity.problems(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        bad = []
    if bad:
        return integrity.IncompletePlugin(iface, bad)
    from .plugin import TerraByteRoadsPlugin

    return TerraByteRoadsPlugin(iface)
