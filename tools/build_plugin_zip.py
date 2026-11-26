#!/usr/bin/env python3
"""Build the installable plugin zip: dist/terrabyte_roads-<version>.zip

The zip contains the ``terrabyte_roads/`` folder only (what QGIS's "Install from ZIP" expects) and a MANIFEST.json
with the SHA-256 of every file. Caches, compiled files and model files are never included; the model runs in
TerraByte's cloud service. Works on Windows, macOS and Linux:

    python tools/build_plugin_zip.py [--out dist]
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "terrabyte_roads"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".ckpt", ".pt", ".pth", ".onnx", ".zip", ".gpkg-journal"}


def version() -> str:
    cp = configparser.ConfigParser()
    cp.read(PKG / "metadata.txt", encoding="utf-8")
    return cp.get("general", "version")


def files():
    for p in sorted(PKG.rglob("*")):
        rel = p.relative_to(ROOT)
        if p.is_dir() or any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        yield p, rel


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args(argv)
    for required in ("metadata.txt", "LICENSE", "icon.png", "__init__.py"):
        if not (PKG / required).is_file():
            print(f"missing terrabyte_roads/{required}", file=sys.stderr)
            return 1
    selected = list(files())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    zpath = out / f"terrabyte_roads-{version()}.zip"
    manifest = {"version": version(),
                "files": {rel.relative_to("terrabyte_roads").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p, rel in selected if rel.name != "MANIFEST.json"}}
    manifest_bytes = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("terrabyte_roads/MANIFEST.json", manifest_bytes)
        for p, rel in selected:
            if rel.name == "MANIFEST.json":
                continue
            z.write(p, rel.as_posix())
    print(f"{zpath} ({len(selected) + 1} files, {zpath.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
