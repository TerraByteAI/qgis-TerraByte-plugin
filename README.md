# TerraByte Road Extractor for QGIS

A QGIS plugin that draws roads, rail, waterways and power lines from your imagery. It finds seven line types
(highway, local road, path, railway, waterway, runway and power line) in an aerial or satellite raster you already
have in QGIS and adds them to the map as lines you can review and export.

## Requirements

- QGIS 3.44, or QGIS 4.2 or later.
- A TerraByte account.
- An internet connection. The imagery inside the rectangle you draw is uploaded to TerraByte's cloud service to run
  the model and is not kept.

## Install

- From the QGIS plugin repository, once the plugin is listed there: **Plugins > Manage and Install Plugins**, then
  search for TerraByte Road Extractor.
- Or build the ZIP yourself with `python tools/build_plugin_zip.py` (it is written to `dist/`), then in QGIS use
  **Plugins > Manage and Install Plugins > Install from ZIP**.

## Use

1. Open the Road Extractor panel from the toolbar and sign in with your TerraByte account.
2. Pick a raster.
3. Draw a rectangle over the area you want.
4. Run the model. Each run uses tiles from your monthly allowance; the run check shows the tiles before you start.
5. Keep the line types you want and delete the lines you do not.
6. Export a GeoPackage, GeoJSON or Shapefile layer. Each line has the fields `class`, `class_id` and `length_m`.

The cloud service address can be overridden with the `TERRABYTE_ROADS_CLOUD_URL` environment variable or the
`terrabyte_roads/cloud/base_url` plugin setting.

## Issues

Report problems at https://github.com/TerraByteAI/qgis-TerraByte-plugin/issues.

## Licence

Copyright (C) 2026 TerraByte. Licensed under the GNU Affero General Public License v3.0 only (AGPL-3.0-only), see
[LICENSE](LICENSE).

The TerraByte name and the logo files (`terrabyte_roads/icon.png`, `terrabyte_roads/resources/brand/terrabyte.svg`)
are trademarks of TerraByte and are not licensed under the AGPL. The bundled fonts, Inter Tight and IBM Plex Mono,
are licensed under the SIL Open Font License 1.1. See [NOTICE](NOTICE) for details.
