#!/usr/bin/env python3
"""Check the zip built by tools/build_plugin_zip.py: the required files are in it and nothing unwanted is.

    python tools/check_plugin_zip.py [dist-dir-or-zip]
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REQUIRED = ("terrabyte_roads/metadata.txt", "terrabyte_roads/__init__.py",
            "terrabyte_roads/icon.png")


def main(argv=None) -> int:
    arg = Path((argv if argv is not None else sys.argv[1:] or ["dist"])[0])
    zips = sorted(arg.glob("terrabyte_roads-*.zip")) if arg.is_dir() else [arg]
    if len(zips) != 1:
        print(f"expected exactly one zip, found {zips}")
        return 1
    zf = zipfile.ZipFile(zips[0])
    names = zf.namelist()
    problems = [f"missing {n}" for n in REQUIRED if n not in names]
    problems += [f"unwanted {n}" for n in names
                 if n.endswith((".pyc", ".pt", ".ckpt", ".pth")) or "__pycache__" in n or "\\" in n
                 or not n.startswith("terrabyte_roads/")
                
                 or n.endswith(".onnx") or n.startswith("terrabyte_roads/models/")]
    for p in problems:
        print(p)
    print(f"{zips[0].name}: {len(names)} entries, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
