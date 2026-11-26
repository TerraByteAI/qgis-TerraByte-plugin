"""Time estimates for a run.

A time is shown only when it rests on a real value: the service's ``progress.eta_s`` while a run is running, the
per-tile time the service publishes in ``/v1/meta`` (``sec_per_tile``), or the runs this plugin has measured (each
from the Extract click to its lines on screen to keep, upload included).  With none of those ``takes_seconds``
returns None and no time is shown.

The estimate for a run is fitted from the last HISTORY_N runs the plugin measured and kept in the settings.  It is
never faster per tile than the service's own figure when the service sends one, never shorter than a remembered run
of that size or smaller, and has MARGIN added on top, rounded up to whole seconds.  It grows with the tile count: for
the same history a larger run never gets a shorter estimate.

A large run (``is_large``: from LARGE_RUN_TILES tiles, or an estimate of a minute or more) is one for which the Draw
screen says roughly how long it takes.
"""
from __future__ import annotations

import json
import math
from typing import Any, List, Optional, Tuple

DEFAULT_SEC_PER_TILE = 0.7
JOIN_SECONDS = 0.0
KEY = "cloud/sec_per_tile"
HISTORY_KEY = "cloud/run_times"
KEEP_KEY = "cloud/run_times_keep"
LEGACY_EXTRA_S = 3.0
BIG_RUN_TILES = 16
HISTORY_N = 8
DEFAULT_OVERHEAD_S = 5.0
MARGIN = 0.30
LEGACY_SAMPLE_TILES = 16
LARGE_RUN_TILES = 256
LARGE_RUN_SECONDS = 60.0
REACH = 4


def per_tile(settings: Any) -> float:
    try:
        v = float(settings._s.value(settings.PREFIX + KEY, 0) or 0)
    except Exception:
        v = 0.0
    return v if 0.05 <= v <= 60 else DEFAULT_SEC_PER_TILE


def recorded_per_tile(settings: Any) -> Optional[float]:
    """The moving average of this plugin's finished runs, or None before the first one."""
    try:
        v = float(settings._s.value(settings.PREFIX + KEY, 0) or 0)
    except Exception:
        return None
    return v if 0.05 <= v <= 60 else None


def service_per_tile(meta: Any) -> Optional[float]:
    """Seconds per tile the service published in /v1/meta, if it is a real positive number."""
    try:
        v = float((meta or {}).get("sec_per_tile"))
    except (TypeError, ValueError, AttributeError):
        return None
    return v if 0.05 <= v <= 600 else None


def _runs(settings: Any, key: str) -> List[Tuple[int, float]]:
    try:
        raw = json.loads(settings._s.value(settings.PREFIX + key, "") or "[]")
        return [(int(n), float(t)) for n, t in raw if int(n) > 0 and float(t) > 0]
    except Exception:
        return []


def history(settings: Any) -> List[Tuple[int, float]]:
    """The last HISTORY_N finished runs this plugin measured, ``[(tiles, seconds)]``, oldest first: runs timed to the
    lines on screen (KEEP_KEY), after runs stored under the older HISTORY_KEY, which count LEGACY_EXTRA_S longer."""
    legacy = [(n, t + LEGACY_EXTRA_S) for n, t in _runs(settings, HISTORY_KEY)]
    return (legacy + _runs(settings, KEEP_KEY))[-HISTORY_N:]


def big_run_rate(runs: List[Tuple[int, float]]) -> Optional[float]:
    """Seconds per tile of the slowest of the largest remembered runs (at least BIG_RUN_TILES tiles and at least half
    the largest), fixed part included; None without such a run.  ``_base`` uses ``tiles x`` this rate as a floor."""
    big = [(n, t) for n, t in runs if n >= BIG_RUN_TILES]
    if not big:
        return None
    top = max(n for n, _ in big)
    return max(t / n for n, t in big if 2 * n >= top)


def model(settings: Any, meta: Any = None) -> Optional[Tuple[float, float]]:
    """``(overhead_s, sec_per_tile)``: a run of n tiles takes ``overhead + n x rate``; None when nothing real is known.

    The rate is never faster than the service's.  With runs of clearly different sizes it is the least-squares slope
    of their times; otherwise the service's rate (or, without one, the fastest per-tile time measured).  The overhead
    is the median of what the remembered runs took beyond ``rate x tiles``; ``takes_seconds`` adds the longest run of
    that size or smaller as a floor, so no measured run took longer than its estimate."""
    floor = service_per_tile(meta)
    runs = history(settings)
    if not runs:
        legacy = recorded_per_tile(settings)
        if legacy is not None:
            runs = [(LEGACY_SAMPLE_TILES, LEGACY_SAMPLE_TILES * legacy)]
        elif floor is None:
            return None
        else:
            return DEFAULT_OVERHEAD_S, floor
    rate = None
    ns = [n for n, _ in runs]
    if len(runs) >= 2 and max(ns) >= 2 * min(ns):
        mn = sum(ns) / len(ns)
        mt = sum(t for _, t in runs) / len(runs)
        sxx = sum((n - mn) ** 2 for n in ns)
        if sxx > 0:
            rate = sum((n - mn) * (t - mt) for n, t in runs) / sxx
    if rate is None or rate <= 0:
        rate = floor if floor is not None else min(t / n for n, t in runs)
    if floor is not None:
        rate = max(rate, floor)
    rate = max(0.01, rate)
    overhead = max(0.0, _median([t - rate * n for n, t in runs]))
    return overhead, rate


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    k = len(s) // 2
    return s[k] if len(s) % 2 else 0.5 * (s[k - 1] + s[k])


def slowest_up_to(runs: List[Tuple[int, float]], tiles: int) -> float:
    """The longest any remembered run of ``tiles`` tiles or fewer took (0 without one).  A run is never estimated
    shorter than that: not shorter than a run of its own size, and not shorter than a smaller one."""
    return max([t for n, t in runs if n <= tiles], default=0.0)


def real_per_tile(settings: Any, meta: Any = None) -> Optional[float]:
    """The per-tile part of the model (never faster than the service's rate); None when nothing real is known."""
    m = model(settings, meta)
    return None if m is None else m[1]


def takes_seconds(settings: Any, tiles: int, meta: Any = None) -> Optional[float]:
    """How long a run of ``tiles`` takes from the Extract click to its lines on screen to keep, or None when there is
    no real value; MARGIN on top, in whole seconds rounded up.

    Between the smallest and the largest remembered sizes it is the straight line between the remembered runs either
    side (``between``).  Otherwise, up to the reach (REACH times the larger of the largest remembered run and
    BIG_RUN_TILES), it is the fitted ``model`` floored by ``big_run_rate`` and ``slowest_up_to``, and below the smallest
    remembered size never more than that run took; beyond the reach it is the estimate at the reach times
    sqrt(tiles / reach).  Never faster per tile than the service's ``sec_per_tile`` when it sends one."""
    m = model(settings, meta)
    if m is None:
        return None
    runs = history(settings)
    reach = REACH * max(max((n for n, _ in runs), default=0), BIG_RUN_TILES) if runs else 0
    floor = service_per_tile(meta)
    if reach and tiles > reach:
        base = _base(m, runs, reach) * math.sqrt(tiles / reach)
        if floor is not None:
            base = max(base, m[0] + tiles * floor)
    else:
        pts = envelope(runs)
        mid = between(pts, tiles)
        if mid is not None:
            base = mid
        else:
            base = _base(m, runs, tiles)
            if pts and tiles < pts[0][0]:
                base = min(base, pts[0][1])
        if floor is not None:
            base = max(base, tiles * floor)
    return float(math.ceil(base * (1.0 + MARGIN) - 1e-9))


def envelope(runs: List[Tuple[int, float]]) -> List[Tuple[int, float]]:
    """One point per remembered size, ``(tiles, the longest any remembered run of that many tiles or fewer took)``,
    smallest first; the times never fall as the size grows."""
    return [(n, slowest_up_to(runs, n)) for n in sorted({n for n, _ in runs})]


def between(pts: List[Tuple[int, float]], tiles: int) -> Optional[float]:
    """The straight line between the two points of ``envelope`` either side of ``tiles`` (a point's own time at its
    size); None below the smallest or above the largest remembered size."""
    if not pts or tiles < pts[0][0] or tiles > pts[-1][0]:
        return None
    for (n0, t0), (n1, t1) in zip(pts, pts[1:]):
        if tiles <= n1:
            return t0 + (t1 - t0) * (tiles - n0) / (n1 - n0)
    return pts[-1][1]


def _base(m: Tuple[float, float], runs: List[Tuple[int, float]], tiles: int) -> float:
    """The model for ``tiles`` within the reach of the remembered runs (before MARGIN)."""
    base = m[0] + tiles * m[1]
    rb = big_run_rate(runs)
    if rb is not None:
        base = max(base, tiles * rb)
    return max(base, slowest_up_to(runs, tiles))


def left_seconds(settings: Any, meta: Any, todo: int, eta_s: Optional[float] = None,
                 run_per_tile: Optional[float] = None) -> Optional[float]:
    """How long the ``todo`` tiles still to run take, while a run runs.

    The service's ``eta_s`` when it sends one, never less than the measured rate: this run's own seconds per finished
    tile (``run_per_tile``, once it has one) or else the slower of the service's and the recorded per-tile time.
    None when nothing is left or there is no real value."""
    if todo <= 0:
        return None
    if run_per_tile is not None and run_per_tile > 0:
        vals = [todo * run_per_tile]
    else:
        rate = real_per_tile(settings, meta)
        vals = []
        if rate is not None:
            cap = takes_seconds(settings, todo, meta)
            vals = [min(todo * rate, cap) if cap is not None else todo * rate]
    try:
        if eta_s is not None and float(eta_s) >= 0:
            vals.append(float(eta_s))
    except (TypeError, ValueError):
        pass
    return max(vals) if vals else None


READ_KEY = "cloud/read_s_per_msample"
DEFAULT_READ_S_PER_MSAMPLE = 0.02


def _scale(area: Any) -> float:
    try:
        g, im = float(getattr(area, "gsd_m", 0) or 0), float(getattr(area, "image_m", 0) or 0)
    except (TypeError, ValueError):
        return 1.0
    return g / im if g > 0 and im > 0 else 1.0


def tile_msamples(area: Any) -> float:
    """Millions of source samples (pixels x 3 bands) one tile of ``area`` covers."""
    f = max(1.0, _scale(area))
    return (512.0 * f) ** 2 * 3 / 1e6


def read_rate(settings: Any) -> float:
    """Seconds per million source samples read at a coarser resolution, as measured on this computer (else the default)."""
    try:
        v = float(settings._s.value(settings.PREFIX + READ_KEY, 0) or 0)
    except Exception:
        v = 0.0
    return v if 0.001 <= v <= 1.0 else DEFAULT_READ_S_PER_MSAMPLE


READ_STEP = 1.4


def record_read(settings: Any, seconds: float, msamples: float) -> None:
    """Remember how long reading ``msamples`` million source samples took (half old, half new).  One run moves the
    remembered rate by at most a factor of READ_STEP either way, so one slow run (another program busy, a second QGIS)
    cannot double the next estimate."""
    if settings is None or seconds <= 0 or msamples <= 0:
        return
    new = max(0.001, min(1.0, seconds / msamples))
    old = read_rate(settings)
    rate = min(old * READ_STEP, max(old / READ_STEP, 0.5 * old + 0.5 * new))
    try:
        settings._s.setValue(settings.PREFIX + READ_KEY, round(rate, 5))
    except Exception:
        pass


def read_extra_per_tile(settings: Any, area: Any) -> float:
    """Seconds a tile of ``area`` takes to read beyond a tile at its image's own resolution (0 at its own)."""
    f = _scale(area)
    if f <= 1.0 + 1e-9:
        return 0.0
    return (tile_msamples(area) - 512.0 ** 2 * 3 / 1e6) * read_rate(settings)


def read_extra_of(area: Any, read_s: float) -> float:
    """Of ``read_s`` seconds a run spent reading its tiles, the part beyond reading them at the image's own resolution."""
    f = _scale(area)
    if f <= 1.0 + 1e-9 or read_s <= 0:
        return 0.0
    return read_s * (1.0 - 1.0 / (f * f))


def takes_for_area(settings: Any, area: Any, meta: Any = None) -> Optional[float]:
    """``takes_seconds`` for ``area``'s tiles plus, at a coarser resolution than its image's, the extra reading (with
    MARGIN), in whole seconds rounded up; None when there is no real value."""
    n = int(getattr(area, "tile_count", 0) or 0)
    base = takes_seconds(settings, n, meta)
    if base is None:
        return None
    extra = n * read_extra_per_tile(settings, area)
    return float(math.ceil(base + extra * (1.0 + MARGIN) - 1e-9))


def is_large(tiles: int, seconds: Optional[float] = None) -> bool:
    """A run big enough that the Draw screen says how long it takes (see the module notes)."""
    return tiles >= LARGE_RUN_TILES or (seconds is not None and seconds >= LARGE_RUN_SECONDS)


def record(settings: Any, tiles: int, seconds: float) -> None:
    """Remember one finished run (tiles, seconds from the Extract click to the end) for the model; the last HISTORY_N
    are kept.  The moving average of seconds per tile (KEY) is kept up to date too (half old, half new)."""
    if settings is None or tiles <= 0 or seconds <= 0:
        return
    runs = _runs(settings, KEEP_KEY) + [(int(tiles), round(float(seconds), 2))]
    new = max(0.05, min(60.0, seconds / tiles))
    old = per_tile(settings)
    try:
        settings._s.setValue(settings.PREFIX + KEEP_KEY, json.dumps([list(r) for r in runs[-HISTORY_N:]]))
        settings._s.setValue(settings.PREFIX + KEY, round(0.5 * old + 0.5 * new, 3))
    except Exception:
        pass


def run_seconds(settings: Any, tiles: int) -> float:
    return tiles * per_tile(settings) + JOIN_SECONDS


def fmt(seconds: Optional[float]) -> str:
    """'~N s' under a minute, '~N min' above (never 0), rounded up so a time is never shown shorter than it is."""
    if seconds is None:
        return ""
    s = max(1, math.ceil(seconds - 1e-9))
    if s < 60:
        return f"~{s} s"
    return f"~{max(1, math.ceil(seconds / 60.0 - 1e-9))} min"


def fmt_left(seconds: Optional[float]) -> str:
    """The time left while a run runs: '~N s' under a minute, '~N min M s' (rounded up to 10 s) under 10 minutes, so it
    visibly counts down while the run goes on; '~N min' from 10 minutes."""
    if seconds is None:
        return ""
    s = max(1, math.ceil(seconds - 1e-9))
    if s < 60:
        return f"~{s} s"
    if s < 600:
        s = int(math.ceil(s / 10.0) * 10)
        m, r = divmod(s, 60)
        return f"~{m} min" if r == 0 else f"~{m} min {r} s"
    return f"~{max(1, math.ceil(seconds / 60.0 - 1e-9))} min"


def about(seconds: Optional[float]) -> str:
    """The same estimate in words for a sentence: 'about 30 seconds', 'about a minute', 'about 6 minutes',
    'about an hour', 'about 3 hours'; '' when there is none."""
    if seconds is None:
        return ""
    if seconds < 60:
        s = max(5, int(math.ceil(seconds / 5.0 - 1e-9)) * 5)
        return "about a minute" if s >= 60 else f"about {s} seconds"
    if seconds < 50 * 60:
        m = max(1, math.ceil(seconds / 60.0 - 1e-9))
        return "about a minute" if m == 1 else f"about {m} minutes"
    if seconds < 90 * 60:
        return "about an hour"
    return f"about {int(round(seconds / 3600.0))} hours"
