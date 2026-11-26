# Bundled fonts

The Road Extractor UI uses two typefaces. Both are licensed under the SIL Open Font License 1.1
(full texts: `OFL-InterTight.txt`, `OFL-IBMPlexMono.txt`).

| Files | Font | Source | Notes |
|---|---|---|---|
| `InterTight-Light/Regular/Medium.ttf` | Inter Tight 300/400/500 (the weights the UI uses) | github.com/google/fonts `ofl/intertight` (`InterTight[wght].ttf`) | static instances cut from the variable font with fontTools `instancer` (Qt 5 only reads the default instance of a variable font); no Reserved Font Name |
| `IBMPlexMono-Regular/Medium.ttf` | IBM Plex Mono 400/500 | github.com/google/fonts `ofl/ibmplexmono` | unmodified (the licence reserves the name "Plex") |

They are loaded with `QFontDatabase.addApplicationFont` when the panel is built (`ui/theme.py: load_fonts`). If loading
fails the style sheets fall back to the system fonts.
