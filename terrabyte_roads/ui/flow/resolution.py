"""The resolution a run runs at (the Start screen's Resolution card), and what follows from it on Draw, the run
check and the not-enough-tiles page.

The choice lives in ``ctx.extras['resolution']`` (a ``Choice``) for ONE raster layer (its id and file).  It is never put
on ``ctx.imagery``: that is the layer as the file has it and is replaced by a fresh ``inspect_layer`` in several places
(Start, the run's gate, the Recap's picker).  So:
* "New area" on the Recap (same raster) finds the choice and Draw keeps it;
* picking a different raster (Start or the Recap) finds no choice for it: both fields start again from that raster's
  own values (the image's resolution from the file, Run at = the image's own);
* an area is made at the choice (``run_imagery``) and keeps what it was made at (``AreaChoice.gsd_m``, ``image_m``):
  the run, the run check and the summary line read the area, never the card.

Rules: "Image resolution" in cm per pixel (m per pixel for an image of 1 m or coarser), pre-filled from the file,
always editable; "Run at": the image's own (pre-selected), 60 cm, 30 cm, Custom (1 cm to 1 m); never finer than the
image; the image's own is always valid, even coarser than 1 m.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from ...cloud.grid import (AreaChoice, ImageryInfo, area_tiles_label, at_resolution, compute_grid, number_text,
                           res_label, side_label, whole_image_geometry)

KEY = "resolution"
PRESETS = (0.6, 0.3)
RECOMMENDED_M = 0.6
CUSTOM_MIN_M, CUSTOM_MAX_M = 0.01, 1.0
IMAGE_MIN_M = 0.01
TOL = 1e-9

HINT = "Works best at 30 to 60 cm per pixel."
UNKNOWN_TEXT = "This file does not include its resolution. Enter it to go on."
RANGE_TEXT = "Enter 1 cm to 1 m."
CUSTOM_RANGE = "1 cm to 1 m"
FINER_OPTION = "Finer than the image"
SAME = "same as the image"
CHANGE_TIP = "Back to the Resolution card. Clears the drawn area."
CLEARS_TEXT = "Changing resolution clears the area."
START_TIP = "Check the resolution to go on"
FROM_FILE, NOT_IN_FILE, EDITED = "From the file", "Not in the file", "Edited"
USE_FILE = "Use the file's value"
IMAGE_MIN_TEXT = "Enter 1 cm or more."


def finer_text(image_m: float) -> str:
    r = res_label(image_m)
    return f"This image is {r} per pixel. Enter {r} or coarser."


def coarse_note(image_m: float) -> str:
    return f"Results are best at 30 to 60 cm per pixel. This image runs at its own {res_label(image_m)}."


def file_says(file_m: float) -> str:
    return f"The file says {res_label(file_m)}."


def parse_number(text: str) -> Optional[float]:
    """A positive number typed in a field ('10', '15.5', '15,5'); None for anything else."""
    t = (text or "").strip().replace(",", ".")
    if not t:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= TOL * max(abs(a), abs(b), 1e-300)


def field_text(m: float, unit: str) -> str:
    """The image resolution as the field shows it: '10' (cm), '15.5' (cm), '2' (m)."""
    return number_text(m, 2) if unit == "m" else number_text(m * 100.0, 1)


@dataclass
class Choice:
    """The Resolution card's state for one raster layer (see the module notes)."""
    layer_id: str = ""
    source: str = ""
    file_m: float = 0.0
    unit: str = "cm"
    image_text: str = ""
    pick: str = "own"
    custom_text: str = ""

    @classmethod
    def for_image(cls, info: ImageryInfo) -> "Choice":
        file_m = float(info.gsd_m) if getattr(info, "gsd_in_file", True) and info.gsd_m > 0 else 0.0
        unit = "m" if file_m >= 1.0 - TOL else "cm"
        return cls(layer_id=info.layer_id, source=info.source, file_m=file_m, unit=unit,
                   image_text=field_text(file_m, unit) if file_m else "")

    def belongs_to(self, info: ImageryInfo) -> bool:
        """The same raster layer, file and file value (a file that changed starts again from its own values)."""
        file_m = float(info.gsd_m) if getattr(info, "gsd_in_file", True) and info.gsd_m > 0 else 0.0
        same_value = _same(self.file_m, file_m) if (file_m and self.file_m) else self.file_m == file_m
        return self.layer_id == info.layer_id and self.source == info.source and same_value

    @property
    def edited(self) -> bool:
        """The user changed the file's value (typing it back is not an edit)."""
        return bool(self.file_m) and self.image_text.strip() != field_text(self.file_m, self.unit)

    def state_label(self) -> str:
        if not self.file_m:
            return NOT_IN_FILE
        return EDITED if self.edited else FROM_FILE

    def image_m(self) -> Optional[float]:
        """The image's own resolution the run takes (metres): the file's exact value unless the user changed it."""
        if self.file_m and not self.edited:
            return self.file_m
        v = parse_number(self.image_text)
        if v is None:
            return None
        m = v if self.unit == "m" else v / 100.0
        return m if m >= IMAGE_MIN_M - TOL else None

    def image_problem(self) -> str:
        """What the image field says under it when it holds no usable value ('' when it does)."""
        if self.image_m() is not None:
            return ""
        if not self.image_text.strip():
            return UNKNOWN_TEXT if not self.file_m else ""
        return IMAGE_MIN_TEXT

    def finer(self, m: float) -> bool:
        """``m`` is finer than the image (such an option is off; the image's own is never)."""
        im = self.image_m()
        return im is not None and m < im and not _same(m, im)

    def custom_m(self) -> Optional[float]:
        v = parse_number(self.custom_text)
        return None if v is None else v / 100.0

    def custom_problem(self) -> str:
        if self.pick != "custom":
            return ""
        m, im = self.custom_m(), self.image_m()
        if m is None or m < CUSTOM_MIN_M - TOL or m > CUSTOM_MAX_M + TOL:
            return RANGE_TEXT
        if im is not None and self.finer(m):
            return finer_text(im)
        return ""

    def target_m(self) -> Optional[float]:
        """The resolution the run runs at (metres), None while the card is not settled."""
        im = self.image_m()
        if im is None:
            return None
        if self.pick == "own":
            return im
        if self.pick == "custom":
            m = self.custom_m()
            if self.custom_problem() or m is None:
                return None
            return im if _same(m, im) else m
        m = float(self.pick)
        return None if self.finer(m) else m

    @property
    def valid(self) -> bool:
        return self.target_m() is not None

    def custom_off(self) -> bool:
        """Custom is off when every value it takes (1 cm to 1 m) is finer than the image."""
        return self.finer(CUSTOM_MAX_M)

    def preset_listed(self, m: float) -> bool:
        """A preset that reads the same as the image's own ('60 cm · same as the image') is not listed a second time:
        the image's own stands for it."""
        im = self.image_m()
        return im is None or res_label(im) != res_label(m)

    def run_at_keys(self) -> list:
        """The Run at options, in order: the image's own, the presets still listed, Custom."""
        return ["own"] + [k for k in ("0.6", "0.3") if self.preset_listed(float(k))] + ["custom"]

    def settle_pick(self) -> None:
        """A preset that became finer than the image (the image resolution was changed) gives way to the image's own;
        so does one no longer listed because the image's own reads the same."""
        if self.pick in ("0.6", "0.3") and (self.finer(float(self.pick)) or not self.preset_listed(float(self.pick))):
            self.pick = "own"
        if self.pick == "custom" and self.custom_off():
            self.pick = "own"

    def own_text(self) -> str:
        im = self.image_m()
        return f"{res_label(im)} · {SAME}" if im is not None else "Same as the image"

    def hint(self, info: Optional[ImageryInfo] = None) -> str:
        """The line under the card: the tile at the run's resolution (east-west by north-south: they differ in
        degrees), then the advice (not for a coarse image, whose note says it; only the advice while the card is not
        settled)."""
        t, im = self.target_m(), self.image_m()
        if t is None or im is None:
            return HINT
        w = h = 512 * t
        if info is not None:
            try:
                ri = at_resolution(info, im, t)
                w, h = ri.tile_side_m, ri.tile_h_m
            except ValueError:
                pass
        tile = f"A tile covers {side_label(w)} × {side_label(h)} m."
        return tile if im > 1.0 + TOL else f"{tile} {HINT}"

    def note(self) -> str:
        """The note under the image field for a coarse image (coarser than 1 m)."""
        im = self.image_m()
        return coarse_note(im) if (im is not None and im > 1.0 + TOL) else ""


def choice_for(ctx, info: Optional[ImageryInfo]) -> Optional[Choice]:
    """The card's state for ``info``: the one kept for this raster, else its own values (kept from now on)."""
    if info is None:
        return None
    ch = ctx.extras.get(KEY)
    if isinstance(ch, Choice) and ch.belongs_to(info):
        return ch
    ch = Choice.for_image(info)
    ctx.extras[KEY] = ch
    return ch


def run_imagery(ctx) -> Optional[ImageryInfo]:
    """The imagery as the run sees it at the chosen resolution (what Draw tiles), None while the card is not settled."""
    info = getattr(ctx, "imagery", None)
    ch = choice_for(ctx, info)
    if ch is None or not ch.valid:
        return None
    ri = at_resolution(info, ch.image_m(), ch.target_m())
    ri.image_from = "user" if (ch.edited or not ch.file_m) else "file"
    ri.target_choice = {"own": "image", "0.6": "60cm", "0.3": "30cm"}.get(ch.pick, "custom")
    return ri


def summary(gsd_m: float, image_m: float) -> str:
    """'Runs at 60 cm · image is 10 cm', 'Runs at 20 cm · same as the image' (Draw, the run check, not enough tiles)."""
    if _same(gsd_m, image_m):
        return f"Runs at {res_label(gsd_m)} · {SAME}"
    return f"Runs at {res_label(gsd_m)} · image is {res_label(image_m)}"


def area_summary(area: AreaChoice) -> str:
    return summary(area.gsd_m, area.image_m)


def recommended(ctx, area: Optional[AreaChoice]) -> Optional[AreaChoice]:
    """The same area at 60 cm, when the run runs finer than that (the comparison line); None otherwise."""
    info = getattr(ctx, "imagery", None)
    if area is None or info is None or not area.tile_count or area.gsd_m >= RECOMMENDED_M - TOL:
        return None
    key = (round(area.gsd_m, 12), round(area.image_m, 12), area.geometry_wkt, area.source)
    hit: Any = getattr(area, "_recommended", None)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        from qgis.core import QgsGeometry

        ri = at_resolution(info, area.image_m, RECOMMENDED_M)
        g = whole_image_geometry(ri) if area.whole_image else QgsGeometry.fromWkt(area.geometry_wkt)
        a60 = compute_grid(ri, g, source=area.source)
    except Exception:
        a60 = None
    try:
        area._recommended = (key, a60)
    except AttributeError:
        pass
    return a60 if (a60 is not None and a60.tile_count) else None


def recommended_count(a60: AreaChoice) -> str:
    """'36 tiles at 60 cm' ('up to 36 tiles at 60 cm' while some may hold no imagery)."""
    return f"{area_tiles_label(a60)} at {res_label(RECOMMENDED_M)}"
