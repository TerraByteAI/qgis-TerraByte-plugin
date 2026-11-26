"""RunController: create a run, upload its tiles, start it, follow its events, stop it, keep its lines.

One object per run, owned by the flow controller (``ctx.run``); everything network-side goes through ``ctx.client``.
It runs on the main thread, except ``TileReader``: the tiles are read and PNG-encoded in one worker thread that uses
only GDAL (its own dataset handle per tile), numpy and zlib, touches no Qt or QGIS object, stops between pieces of a
tile when told, and hands finished tiles to the main thread through a queue drained by a main-thread timer.

Every tile is uploaded, then the run is started.  The lines of each ``tile_done`` event are kept as they arrive; at
the run's end its result is fetched, the service is asked to delete its copy where it supports that
(``acknowledge_result``), and when the result cannot be had the review is built from the lines kept
(``held_result``).  A streaming flow (start first, upload while the run extracts) is in the code behind
``STREAMING_ENABLED``, which is off.

Signals
    changed()                      any state change (screens refresh from the attributes)
    tile_done(row, col, lines)     a tile finished (lines in run pixel coordinates)
    phase(str)                     'uploading' | 'extracting'   (a ``joining`` state is treated as extracting)
    finished(state)                'done' | 'stopped' | 'failed' | 'nothing_found'
    problem(kind, ApiError|None)   'over_budget' | 'consent_required' | 'busy' | 'network' | 'deleted' (410
                                   run_data_deleted) | 'other'
"""
from __future__ import annotations

import queue
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from .client import ApiError
from .grid import AreaChoice, ImageryInfo, keep_tiles, read_tile_rgb, tile_has_imagery, tile_png, wavefront_order
from .runref import RunRef
from .tilereader import READ_AHEAD, TileReader

END_STATES = ("done", "stopped", "failed", "nothing_found")
UPLOAD_PARALLEL = 8
UPLOAD_REFRESH_MS = 100
SEND_S_PER_TILE = 0.05
PACE_TILES = 8
DRAIN_MS = 15
READER_JOIN_S = 1.0
STREAM_UPLOAD_PARALLEL = 4
STREAM_UPLOAD_TIMEOUT_S = 50.0
TILE_RETRY_S = (2.0, 5.0, 10.0)
RATE_WAIT_S = (1.0, 60.0)
RATE_WAITS_MAX = 20
EVENTS_RETRY_S = (0.5, 2.0, 5.0, 10.0, 20.0)
STOP_RETRY_S = (2.0, 5.0, 10.0)
CHARGE_WAIT_S = 20.0
END_READ_TRIES = 5
READ_TEXT = "Couldn't read the imagery file. Check that it is still there and that QGIS can open it."


def _log(msg: str) -> None:
    try:
        from qgis.core import Qgis, QgsMessageLog

        from ..core import logbuffer

        logbuffer.record(msg)
        QgsMessageLog.logMessage(str(msg), "Road Extractor", Qgis.MessageLevel.Warning)
    except Exception:
        pass


ACK_KEY = "result_delete"
ACK_RETRY_MS = 60000
ACK_STATES = ("done", "stopped", "nothing_found", "failed")


def zero_retention(ctx) -> bool:
    """True when the service's ``meta`` carries the entry this function is named after: the service then deletes a
    run's lines once the plugin has them.  Older services without it have no deletion route; nothing is sent to them."""
    meta = getattr(ctx, "meta", None) or {}
    return isinstance(meta, dict) and isinstance(meta.get("zero_retention"), dict)


def acknowledge_result(ctx, run_id: str, state: str, _retry: bool = False) -> bool:
    """Send ``DELETE /v1/runs/{run_id}/result`` once for an ended run whose lines the plugin has (at its end for a
    failed or nothing_found run, after the result's fetch for a done or stopped one), in the background.  Answers: 204
    done; 409 not retried (a later end of the same run sends it again); 404 ``not_found`` dropped; 404 or 405 without
    that code (no such route) nothing more this session; 401 dropped and the user stays signed in; 426, 429, 5xx or no
    answer: one retry ``ACK_RETRY_MS`` later, then dropped.  Never shows an error, never raises.  True when a request
    went out."""
    try:
        if not run_id or state not in ACK_STATES or not zero_retention(ctx):
            return False
        st = ctx.extras.setdefault(ACK_KEY, {"off": False, "runs": {}})
        runs = st["runs"]
        cur = runs.get(run_id)
        if st["off"] or cur in ("sending", "done", "dropped") or (cur == "retrying" and not _retry):
            return False
        client = getattr(ctx, "client", None)
        if client is None or not hasattr(client, "delete_result"):
            return False
        import weakref

        ref = weakref.ref(ctx)
        runs[run_id] = "sending"

        def answered(err) -> None:
            if err is None:
                runs[run_id] = "done"
                return
            status, code = int(getattr(err, "status", 0) or 0), str(getattr(err, "code", "") or "")
            if status == 409:
                runs.pop(run_id, None)
            elif status == 404 and code == "not_found":
                runs[run_id] = "dropped"
            elif status in (404, 405):
                st["off"] = True
                runs[run_id] = "dropped"
            elif (status == 0 or status in (426, 429) or status >= 500) and not _retry:
                runs[run_id] = "retrying"
                QTimer.singleShot(ACK_RETRY_MS, lambda: ref() is not None and acknowledge_result(ref(), run_id, state,
                                                                                                _retry=True))
            else:
                runs[run_id] = "dropped"
            _log(f"Result deletion for {run_id}: {code or status} ({runs.get(run_id, 'not sent')})")

        client.delete_result(run_id, ok=lambda _res: answered(None), err=answered)
        return True
    except Exception as e:
        _log(f"Result deletion not sent ({type(e).__name__})")
        return False


def resolution_facts(area: AreaChoice) -> Optional[Dict[str, Any]]:
    """The run's optional ``resolution`` field: the image's resolution as used and where it came from (file or
    user), the resolution the run is made at and which option chose it (the image's own, 60 cm, 30 cm, custom), and the
    resampling.  None (the field is left out of the request) when a fact breaks the
    rules checked here, which the Resolution card already enforces."""
    target, image = float(area.gsd_m or 0), float(getattr(area, "image_m", 0) or area.gsd_m or 0)
    choice = getattr(area, "target_choice", "image") or "image"
    facts = {"image_gsd_m": image, "image_gsd_from": getattr(area, "image_from", "file") or "file",
             "target_gsd_m": target, "target": choice,
             "resampling": "none" if abs(target - image) <= 1e-9 * max(target, image) else "average"}
    ok = (0 < image <= 1000 and target >= image * 0.999 and facts["image_gsd_from"] in ("file", "user")
          and (choice != "image" or abs(target - image) <= 0.001 * image)
          and (choice != "60cm" or abs(target - 0.6) <= 1e-9) and (choice != "30cm" or abs(target - 0.3) <= 1e-9)
          and (choice != "custom" or 0.01 - 1e-12 <= target <= 1.0 + 1e-12)
          and choice in ("image", "60cm", "30cm", "custom"))
    if not ok:
        _log(f"Run resolution left out of the request (breaks the contract's rules): {facts}")
        return None
    return facts


STREAM_SAFE_KEY = "uploads_never_held"
STREAM_SAFE_VALUE = True
STREAMING_ENABLED = False


RELEASE_RETRY_S = 10.0
RELEASE_TRIES = 60
RELEASING: Set[str] = set()


def release_reservation(ctx, run_id: str, run=None) -> bool:
    """Stop ``run_id`` (a run that never started) until TerraByte answers; then read the balance again and refresh the
    run's screens.  True when a release started (not already under way)."""
    if not run_id or run_id in RELEASING:
        return False
    client = getattr(ctx, "client", None)
    if client is None or not hasattr(client, "stop_run"):
        return False
    import weakref

    ref_ctx = weakref.ref(ctx)
    ref_run = weakref.ref(run) if run is not None else (lambda: None)
    RELEASING.add(run_id)

    def done() -> None:
        RELEASING.discard(run_id)
        c, r = ref_ctx(), ref_run()
        _log(f"Run {run_id} left before it started: stopped, its reserved tiles come back")
        try:
            if c is not None and not getattr(c, "shut_down", False):
                c.refresh_account()
            if r is not None and not r._dead:
                r.changed.emit()
        except RuntimeError:
            pass

    def attempt(n: int) -> None:
        c = ref_ctx()
        if c is None or getattr(c, "shut_down", False) or getattr(c, "client", None) is None:
            RELEASING.discard(run_id)
            return

        def failed(e) -> None:
            status = int(getattr(e, "status", 0) or 0)
            if status in (404, 409):
                done()
            elif (status == 0 or status == 429 or status >= 500) and n + 1 < RELEASE_TRIES:
                QTimer.singleShot(int(RELEASE_RETRY_S * 1000), lambda: attempt(n + 1))
            else:
                RELEASING.discard(run_id)
                _log(f"Run {run_id} left before it started: Stop not delivered ({getattr(e, 'code', '')}, HTTP {status})")
        try:
            c.client.stop_run(run_id, ok=lambda _r: done(), err=failed, timeout_s=15.0)
        except Exception:
            RELEASING.discard(run_id)
    attempt(0)
    return True


def stream_spec(ctx) -> Optional[Dict[str, Any]]:
    """``meta.stream`` when streaming is switched on and that spec has ``STREAM_SAFE_KEY`` set to
    ``STREAM_SAFE_VALUE``, else None (the non-streaming flow)."""
    if not STREAMING_ENABLED:
        return None
    meta = getattr(ctx, "meta", None) or {}
    spec = meta.get("stream") if isinstance(meta, dict) else None
    if not isinstance(spec, dict) or STREAM_SAFE_KEY not in spec or spec.get(STREAM_SAFE_KEY) != STREAM_SAFE_VALUE:
        return None
    if type(spec.get(STREAM_SAFE_KEY)) is not type(STREAM_SAFE_VALUE):
        return None
    return spec


def stream_order(tiles, rows: int, cols: int, spec: Optional[Dict[str, Any]]) -> List[Tuple[int, int]]:
    """The order to upload a streamed run's tiles in, from ``meta.stream``."""
    spec = spec or {}
    try:
        block = max(1, int(spec.get("block", 8)))
        single = int(spec.get("single_block_up_to", block))
    except (TypeError, ValueError):
        block, single = 8, 8
    b = max(rows, cols, 1) if max(rows, cols) <= single else block
    return sorted((tuple(t) for t in tiles), key=lambda rc: ((rc[0] % b) + (rc[1] % b), rc[0], rc[1]))


def _num(v: Any, default: Any) -> Any:
    """A non-negative real number from the service, else ``default`` (text, NaN and negatives never get through)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f and f >= 0 else default


class RunController(QObject):
    changed = pyqtSignal()
    tile_done = pyqtSignal(int, int, list)
    phase = pyqtSignal(str)
    finished = pyqtSignal(str)
    problem = pyqtSignal(str, object)

    def __init__(self, ctx, imagery: ImageryInfo, area: AreaChoice, classes_kept: List[int]):
        super().__init__()
        self.ctx = ctx
        self.imagery, self.area, self.classes_kept = imagery, area, sorted(classes_kept)
        self.client = ctx.client
        self.run_id: str = ""
        self.state = "idle"
        self.order: List[Tuple[int, int]] = wavefront_order(area.tiles)
        self.order_no: Dict[Tuple[int, int], int] = {rc: i + 1 for i, rc in enumerate(self.order)}
        self.done_tiles: Set[Tuple[int, int]] = set()
        self.started_tiles: Set[Tuple[int, int]] = set()
        self.failed_tiles: Set[Tuple[int, int]] = set()
        self.empty_tiles: Set[Tuple[int, int]] = set()
        self.tile_lines: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        self.uploaded = 0
        self.upload_total = 0
        self._t_upload0: Optional[float] = None
        self.elapsed_s = 0
        self.eta_s: Optional[float] = None
        self.tiles_used: Optional[int] = None
        self.problem_kind = ""
        self.problem_error: Optional[ApiError] = None
        self.result: Optional[Dict[str, Any]] = None
        self.summary: Optional[Dict[str, Any]] = None
        self.wait_s = 20
        self.poll_delay_ms = 0
        self._after = 0
        self._t0 = time.time()
        self._t_end: Optional[float] = None
        self._timed = False
        self._interrupted = False
        self._queue: List[Tuple[int, int]] = []
        self._inflight = 0
        self._net_fail = 0
        self._polling = False
        self._stop_requested = False
        self._stop_call = None
        self._uploads: Set[Any] = set()
        self._dead = False
        self._changed_pending = False
        self.connection_lost = False
        self.ends = 0
        self._end_reported = False
        self.end_taken: Optional[int] = None
        self.review_fetch: Optional[int] = None
        self.checking = False
        self.checked = 0
        self.check_total = 0
        self._check_todo: List[Tuple[int, int]] = []
        self._check_keep: List[Tuple[int, int]] = []
        self._reader: Optional[TileReader] = None
        self._stopped_readers: List[TileReader] = []
        self._drain_timer: Optional[QTimer] = None
        self.turn_max_s = 0.0
        self.read_s = 0.0
        self.tiles_read = 0
        self.streaming = False
        self.uploading = False
        self._stream_upload_begun = False
        self.upload_broken: Optional[ApiError] = None
        self._sending: Dict[Tuple[int, int], bytes] = {}
        self._attempts: Dict[Tuple[int, int], int] = {}
        self._rate_waits: Dict[Tuple[int, int], int] = {}
        self.end_confirmed = False
        self._end_phase: Optional[str] = None
        self._end_reads = 0
        self._pre: Optional[Dict[str, Any]] = None
        self.charge_pending = False
        self.stop_answered = False
        self.stop_unreachable = False
        self._stop_sending = False
        self._following = True
        self._ever_started = False
        self._end_waiters: List[Tuple[Any, Any]] = []
        self._charge_until = 0.0

    @property
    def tiles_total(self) -> int:
        return len(self.order)

    @property
    def tiles_done(self) -> int:
        return len(self.done_tiles)

    @property
    def tiles_decoded(self) -> int:
        """Finished tiles that were processed (not the ones with no imagery, which take no time and cost nothing)."""
        return len(self.done_tiles - self.empty_tiles)

    @property
    def next_tile_no(self) -> int:
        return self.tiles_done + 1

    @property
    def tiles_charged(self) -> int:
        """Tiles this run was charged (the service's figure; else the tiles that finished).  Not this month's total."""
        return int(self.tiles_used) if self.tiles_used is not None else self.tiles_done

    @property
    def running(self) -> bool:
        return self.state in ("uploading", "extracting")

    def elapsed_text(self) -> str:
        end = self._t_end if self._t_end is not None else time.time()
        s = int(max(0.0, end - self._t0))
        return f"{s // 60:02d}:{s % 60:02d}"

    def estimated_left_s(self) -> Optional[float]:
        """The service's ETA, else the tiles still to run at the rate seen so far in this run (or the plugin's
        average seconds per tile before the first tile is done); None once nothing is left to run."""
        if self.eta_s is not None and self.running:
            return float(self.eta_s)
        todo = self.tiles_total - self.tiles_done
        if todo <= 0 or not self.running:
            return None
        from . import estimate

        own = (self.elapsed_s / self.tiles_done) if (self.tiles_done >= 2 and self.elapsed_s) else None
        left = estimate.left_seconds(self.ctx.settings, getattr(self.ctx, "meta", None), todo, None, own)
        return self.with_upload_left(left)

    def with_upload_left(self, left: Optional[float]) -> Optional[float]:
        """The time left (``left``, the run screen's estimate) with the upload still to do: added to it (the run starts
        after the upload), or with streaming the larger of the two (the run extracts while it uploads)."""
        if self.streaming and self.uploading:
            up = self.upload_left_s()
            return up if left is None else max(left, up)
        if self.state == "uploading" and not self.streaming:
            return self._upload_phase_left(left)
        return None if left is None else left + self.read_left_s()

    def _upload_phase_left(self, left: Optional[float]) -> Optional[float]:
        """The time left while the tiles upload and the run has not started (the per-tile figures do not move
        yet): the run check's "Takes" estimate (from the click to the lines) less the time gone since the click, and
        never less than the upload still to send at the pace seen so far plus the service's seconds per tile.  From
        PACE_TILES tiles sent, the pace seen and the extraction only (not "Takes")."""
        from . import estimate

        took = max(0.0, time.time() - self._t0)
        meta = getattr(self.ctx, "meta", None)
        if self.upload_total and self.uploaded >= min(PACE_TILES, self.upload_total):
            spt = _num((meta or {}).get("sec_per_tile") if isinstance(meta, dict) else None, 0.0)
            todo = max(0, self.tiles_total - self.tiles_done)
            try:
                ext = estimate.takes_seconds(self.ctx.settings, todo, meta) if todo else 0.0
                total = estimate.takes_for_area(self.ctx.settings, self.area, meta)
            except Exception:
                ext = total = None
            sent_for = max(0.0, time.time() - (getattr(self, "_t_upload0", None) or self._t0))
            rest = sent_for / self.uploaded * (self.upload_total - self.uploaded)
            floor = rest + todo * spt
            pace = rest + max(float(ext or 0.0), todo * spt)
            if total is not None:
                pace = min(pace, float(total) - took)
            return max(1.0, floor, pace)
        vals = []
        try:
            total = estimate.takes_for_area(self.ctx.settings, self.area, meta)
        except Exception:
            total = None
        if total is not None:
            vals.append(float(total) - took)
        if self.uploaded >= 2 and self.upload_total:
            spt = _num((meta or {}).get("sec_per_tile") if isinstance(meta, dict) else None, 0.0)
            vals.append(took / self.uploaded * (self.upload_total - self.uploaded) + self.tiles_total * spt)
        if not vals:
            return None if left is None else left + self.read_left_s()
        return max(1.0, max(vals))

    def upload_left_s(self) -> float:
        """The time the tiles not yet sent still take to read and send (streaming)."""
        from . import estimate

        todo = len(self._queue) + self._inflight
        return todo * (estimate.read_extra_per_tile(self.ctx.settings, self.area) + SEND_S_PER_TILE)

    def read_left_s(self) -> float:
        """While the run uploads, the time still to read the tiles not yet read beyond what reading them at
        the image's own resolution takes (0 at the image's own resolution or once everything is read)."""
        if self.state != "uploading" and not self.uploading:
            return 0.0
        from . import estimate

        todo = len(self._queue) + len(self._check_todo)
        return todo * estimate.read_extra_per_tile(self.ctx.settings, self.area)

    def eta_text(self) -> str:
        """'~N s' / '~N min' (service ETA or the estimate above); '' when nothing is left to run."""
        from . import estimate

        return estimate.fmt(self.estimated_left_s())

    def run_ref(self) -> RunRef:
        a, i = self.area, self.imagery
        return RunRef(crs=a.crs, origin=a.origin, pixel_size=a.pixel_size, rows=a.rows, cols=a.cols,
                      tile_px=a.tile_px, gsd_m=a.gsd_m, gsd_y_m=a.gsd_y_m, layer_name=i.name, layer_id=i.layer_id,
                      layer_source=i.source, area_wkt=a.geometry_wkt, area_km2=a.area_km2,
                      drawn_km2=a.drawn_km2 or a.area_km2, classes_kept=self.classes_kept)

    gate = None

    def _allowed(self, leave: bool = True) -> bool:
        g = RunController.gate
        return True if g is None else bool(g(self, leave))

    def begin(self) -> None:
        if not self._allowed():
            return
        self._t0 = time.time()
        self._interrupted = False
        self._t_end = None
        self._end_reported = False
        self._halt_upload()
        self.streaming = self._stream_upload_begun = False
        self.upload_broken = None
        self._set_going()
        self.state = "uploading"
        if getattr(self.area, "upper_bound", False):
            self._start_check()
            return
        self.changed.emit()
        self._create()

    def _create(self) -> None:
        body = {"imagery": {"gsd_m": self.area.gsd_m},
                "grid": {"rows": self.area.rows, "cols": self.area.cols, "tile_px": self.area.tile_px},
                "tiles": [list(t) for t in sorted(self.area.tiles)],
                "area_km2": self.area.area_km2, "client_ref": self.run_ref().to_client_ref(),
                "classes_kept": self.classes_kept}
        facts = resolution_facts(self.area)
        if facts is not None:
            body["resolution"] = facts
        try:
            from . import terms

            body.update(terms.run_create_fields(self.ctx))
        except Exception as e:
            _log(f"terms fields left out ({type(e).__name__})")
        self.client.create_run(body, ok=self._created, err=self._create_failed)

    def _start_check(self) -> None:
        """Read every tile of the area with the upload's own reader and keep those that hold imagery (not a single
        colour), then create the run with exactly those.  The tiles are read in the reader thread; Stop ends the run
        here, with nothing created and nothing charged."""
        self.checking = True
        self._check_todo = list(self.order)
        self._check_keep = []
        self.checked, self.check_total = 0, len(self._check_todo)
        self.changed.emit()
        self._read(self._check_todo, check=True)

    def _check_tile(self, r: int, c: int, holds: bool) -> None:
        if (r, c) in self._check_todo:
            self._check_todo.remove((r, c))
        if holds:
            self._check_keep.append((r, c))
        self.checked += 1
        self._changed_soon()

    def _check_finished(self) -> None:
        self.checking = False
        self._check_todo = []
        keep_tiles(self.area, self._check_keep)
        self.order = wavefront_order(self.area.tiles)
        self.order_no = {rc: i + 1 for i, rc in enumerate(self.order)}
        if not self.area.tiles:
            self.state = "idle"
            self._t_end = time.time()
            self.problem_kind, self.problem_error = "no_imagery", None
            self.changed.emit()
            self.problem.emit("no_imagery", None)
            return
        self.changed.emit()
        self._create()

    def _check_stopped(self) -> None:
        if self._dead or not self.checking:
            return
        self.checking = False
        self._check_todo = []
        self.tiles_used = 0
        self._ended("stopped")

    def _read(self, tiles, check: bool = False) -> None:
        """Read ``tiles`` (in this order) in a TileReader; the main thread takes them as they come (``_drain``)."""
        self._stop_reader()
        rd = TileReader(self.imagery.source, self.area, tiles, check)
        self._reader = rd
        if self._drain_timer is None:
            self._drain_timer = QTimer(self)
            self._drain_timer.setInterval(DRAIN_MS)
            self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start()
        rd.start()

    def _stop_reader(self, wait: bool = False) -> None:
        rd, self._reader = self._reader, None
        if self._drain_timer is not None:
            try:
                self._drain_timer.stop()
            except RuntimeError:
                pass
        if rd is not None:
            rd.stop()
            rd.taken()
            self.read_s += rd.read_s
            self.tiles_read += rd.read_n
            self._stopped_readers.append(rd)
        if wait:
            self._join_readers()

    def release_file(self) -> None:
        """The raster left the project: nothing reads it after this returns (a stopped reader is waited for while it
        finishes its piece, at most READER_JOIN_S)."""
        self._stop_reader()
        self._join_readers()

    def _join_readers(self) -> None:
        """Wait for the stopped readers to leave their last piece (and close their file), at most READER_JOIN_S in all:
        the run is let go (quit, another project, the raster removed), so nothing of it reads the file afterwards."""
        end = time.monotonic() + READER_JOIN_S
        for rd in self._stopped_readers:
            if rd.is_alive():
                rd.join(max(0.0, end - time.monotonic()))
        left = [rd for rd in self._stopped_readers if rd.is_alive()]
        if left:
            _log(f"A tile reader was still reading {READER_JOIN_S:.0f} s after the run was let go; it stops after its piece")
        self._stopped_readers = left

    def _drain(self) -> None:
        """Main thread, every DRAIN_MS: hand finished tiles to the upload (at most UPLOAD_PARALLEL in flight) or to the
        imagery check.  Work here is small: a queue read and an upload call per tile."""
        t0 = time.perf_counter()
        try:
            self._drain_turn()
        finally:
            self.turn_max_s = max(self.turn_max_s, time.perf_counter() - t0)

    def _drain_turn(self) -> None:
        rd = self._reader
        if rd is None or self._dead:
            if self._drain_timer is not None:
                self._drain_timer.stop()
            return
        if self.checking and self._stop_requested:
            self._stop_reader()
            self._check_stopped()
            return
        while rd.check or self._inflight < (STREAM_UPLOAD_PARALLEL if self.streaming else UPLOAD_PARALLEL):
            try:
                kind, r, c, value = rd.out.get_nowait()
            except queue.Empty:
                return
            if kind == "end":
                self._reader = None
                self.read_s += rd.read_s
                self.tiles_read += rd.read_n
                if self._drain_timer is not None:
                    self._drain_timer.stop()
                if rd.halt.is_set():
                    return
                if rd.check:
                    if self.checking:
                        self._check_finished()
                elif self._inflight == 0 and not self._queue:
                    self._uploads_complete()
                return
            if kind == "error":
                self._read_failed(value)
                return
            rd.taken()
            if rd.check:
                self._check_tile(r, c, bool(value))
                continue
            if (r, c) in self._queue:
                self._queue.remove((r, c))
            self._inflight += 1
            self._send_tile((r, c), value)

    def _read_failed(self, e: Exception) -> None:
        err = ApiError(0, "read_error", READ_TEXT, {"detail": f"{type(e).__name__}: {e}"})
        self._stop_reader()
        if self.checking:
            self.checking = False
            self._check_todo = []
        self._queue.clear()
        if self.streaming and self.uploading:
            self._break_upload(err)
            return
        self._fail_request(err)

    def _created(self, res) -> None:
        self.run_id = res["run_id"]
        self._ever_started = False
        if self._dead or self._stop_requested:
            self._stop_call = self.client.stop_run(self.run_id, ok=lambda _r: None if self._dead else self._poll(),
                                                   err=lambda _e: None if self._dead else self._poll(), timeout_s=4.0)
            return
        self.ctx.set_tiles_left(res.get("tiles_left", 0))
        self.ctx.refresh_account()
        self.phase.emit("uploading")
        if stream_spec(self.ctx) is not None:
            self.streaming = True
            self._start_run()
            return
        self._start_uploads(list(sorted(self.area.tiles)))

    def _create_failed(self, err: ApiError) -> None:
        self._fail_request(err)

    def _fail_request(self, err: ApiError) -> None:
        if self.uploading or self._reader is not None:
            self._halt_upload()
        kind = {"over_budget": "over_budget", "consent_required": "consent_required",
                "busy": "busy", "rate_limited": "busy",
                "run_data_deleted": "deleted"}.get(err.code, "network" if err.code == "network_error" else "other")
        if kind in ("network", "other") and self.run_id and not self._ever_started and not self.tiles_done:
            release_reservation(self.ctx, self.run_id, self)
        self.state = "idle" if self.tiles_done == 0 and not self.run_id else self.state
        self._t_end = time.time()
        detail = (err.extra or {}).get("detail")
        if detail and not str(err.code).startswith("http_"):
            _log(f"Run problem {err.code} (HTTP {err.status}): {detail}")
        self.problem_kind, self.problem_error = kind, err
        self.changed.emit()
        self.problem.emit(kind, err)

    def _start_uploads(self, tiles: List[Tuple[int, int]]) -> None:
        self._queue = list(tiles)
        self.upload_total = len(tiles)
        self.uploaded = 0
        self._t_upload0 = time.time()
        self._inflight = 0
        self.uploading = bool(tiles)
        self.changed.emit()
        if not tiles:
            if not self.streaming:
                self._start_run()
            return
        self._read(tiles)

    def _pump_uploads(self) -> None:
        """Kept for callers that start an upload by name: the reader feeds the upload by itself."""
        if self._reader is None and self._queue and not (self._dead or self._stop_requested):
            self._read(list(self._queue))

    def _track(self, call) -> None:
        """Keep an upload call until it finishes, so a halt can abort it."""
        if call is None or not hasattr(call, "done"):
            return
        self._uploads.add(call)
        try:
            call.done.connect(lambda *_a, c=call: self._uploads.discard(c))
        except (TypeError, RuntimeError):
            pass

    def _abort_uploads(self) -> None:
        """The run is halted (project change, raster removed, quit): the tiles still being sent are aborted, so
        nothing more of this run arrives on the GUI thread while QGIS reads or clears the project."""
        calls, self._uploads = list(self._uploads), set()
        for c in calls:
            try:
                c.cancel()
            except (RuntimeError, AttributeError):
                pass

    def _halt_upload(self) -> None:
        """No more reading or sending for this run: the reader stops within one piece, the queue empties, the tiles in
        flight are aborted."""
        self._stop_reader()
        self._queue.clear()
        self._abort_uploads()
        self._sending.clear()
        self._inflight = 0
        self.uploading = False

    def _changed_soon(self) -> None:
        """At most one ``changed`` per UPLOAD_REFRESH_MS during an upload, not one per tile: every ``changed`` redraws
        the run's tiles on the map, and a run of hundreds of tiles would keep the GUI thread busy for its whole
        upload."""
        if self._changed_pending:
            return
        self._changed_pending = True
        QTimer.singleShot(UPLOAD_REFRESH_MS, self._emit_changed)

    def _emit_changed(self) -> None:
        self._changed_pending = False
        if self._dead:
            return
        try:
            self.changed.emit()
        except RuntimeError:
            pass

    def _all_sent(self) -> bool:
        return not self._queue and self._reader is None and self._inflight == 0

    def _tile_uploaded(self) -> None:
        self._inflight = max(0, self._inflight - 1)
        self.uploaded += 1
        self._changed_soon()
        if self._queue or self._reader is not None:
            return
        if self._inflight == 0:
            self._uploads_complete()

    def _uploads_complete(self) -> None:
        """Every tile of the batch is read and answered: start the run, or, when streaming, nothing more (the run has
        already started)."""
        if self._dead or not self.uploading:
            return
        self.uploading = False
        if self.streaming:
            self.changed.emit()
        else:
            self._start_run()

    def _send_tile(self, rc: Tuple[int, int], png: bytes) -> None:
        """Send one tile (counted in flight by the caller); a failure goes to ``_tile_send_failed``.  While
        streaming, each attempt is given STREAM_UPLOAD_TIMEOUT_S."""
        self._sending[rc] = png
        done = self._tile_uploaded
        kw = {"timeout_s": STREAM_UPLOAD_TIMEOUT_S} if self.streaming else {}
        self._track(self.client.upload_tile(self.run_id, rc[0], rc[1], png,
                                            ok=lambda _res, rc=rc, f=done: self._tile_sent(rc, f),
                                            err=lambda e, rc=rc: self._tile_send_failed(rc, e), **kw))

    def _tile_sent(self, rc: Tuple[int, int], done) -> None:
        if self._sending.pop(rc, None) is None:
            return
        done()

    def _tile_send_failed(self, rc: Tuple[int, int], err: ApiError) -> None:
        """The one place that decides a tile's upload failed for good.  No answer, a timeout or a 5xx: the same tile
        is sent again (TILE_RETRY_S).  429: the service's retry_after_s is waited and the tile sent again (up to
        RATE_WAITS_MAX times); that is not a failure.  409 while streaming: the run ended there; the events say how.
        Anything else, or retries used up, is a broken upload: with streaming the run is stopped, without it the run's
        problem card is shown."""
        png = self._sending.get(rc)
        if png is None or self._dead or self._stop_requested or self.state in END_STATES or not self.uploading:
            self._sending.pop(rc, None)
            self._inflight = max(0, self._inflight - 1)
            return
        r, c = rc
        detail = (err.extra or {}).get("detail") or err.message
        if err.status == 429:
            n = self._rate_waits[rc] = self._rate_waits.get(rc, 0) + 1
            self._interrupted = True
            if n <= RATE_WAITS_MAX:
                wait = min(RATE_WAIT_S[1], max(RATE_WAIT_S[0], _num((err.extra or {}).get("retry_after_s"), 5.0)))
                _log(f"Tile {r}_{c}: upload rate limit (429), sent again in {wait:.0f} s ({n})")
                self._send_again(rc, wait)
                return
        elif err.status == 409 and self.streaming:
            _log(f"Tile {r}_{c}: the run already ended at TerraByte (409): no more uploads")
            self._sending.pop(rc, None)
            self._inflight = max(0, self._inflight - 1)
            self._halt_upload()
            self._poll()
            return
        elif err.status == 0 or err.status == 408 or err.status >= 500:
            n = self._attempts[rc] = self._attempts.get(rc, 0) + 1
            self._interrupted = True
            if n <= len(TILE_RETRY_S):
                _log(f"Tile {r}_{c}: upload failed ({err.code}, HTTP {err.status}: {detail}), "
                     f"sent again in {TILE_RETRY_S[n - 1]:.0f} s (try {n + 1})")
                self._send_again(rc, TILE_RETRY_S[n - 1])
                return
        _log(f"Tile {r}_{c}: upload failed for good ({err.code}, HTTP {err.status}: {detail})")
        self._sending.pop(rc, None)
        self._inflight = max(0, self._inflight - 1)
        if self.streaming:
            self._break_upload(err)
            return
        self._queue.clear()
        self._stop_reader()
        self.uploading = False
        self._fail_request(err)

    def _send_again(self, rc: Tuple[int, int], wait_s: float) -> None:
        import weakref

        ref = weakref.ref(self)

        def again() -> None:
            me = ref()
            if me is None or rc not in me._sending:
                return
            if me._dead or me._stop_requested or me.state in END_STATES or not me.uploading:
                me._sending.pop(rc, None)
                me._inflight = max(0, me._inflight - 1)
                return
            me._send_tile(rc, me._sending[rc])
        QTimer.singleShot(int(wait_s * 1000), again)

    def _break_upload(self, err: ApiError) -> None:
        """An upload broke (the network, a tile that could not be read) while the run streams: stop the run.  It ends
        ``stopped`` with its finished tiles charged and kept (``upload_broken``)."""
        if self.upload_broken is not None or self._dead:
            return
        self.upload_broken = err
        detail = (err.extra or {}).get("detail")
        _log(f"Upload broke mid-run ({err.code}, HTTP {err.status}){': ' + str(detail) if detail else ''}: stopping it")
        self._halt_upload()
        self._stop_requested = True
        if not self.run_id:
            return
        self._send_stop()

    def _send_stop(self, attempt: int = 0) -> None:
        """Send Stop until the service answers (STOP_RETRY_S).  Answered (202, or 409: it had ended): the run is followed
        to its end and its charge.  No answer at all: ``stop_unreachable``; the run ends here (the card says TerraByte
        could not be reached) and whoever waits for its end is told."""
        if self._dead or not self.run_id:
            return
        if attempt == 0:
            if self._stop_sending:
                return
            self._stop_sending = True
        import weakref

        ref = weakref.ref(self)

        def answered(_res=None) -> None:
            me = ref()
            if me is None or me._dead:
                return
            me._stop_sending = False
            me.stop_answered, me.stop_unreachable = True, False
            me._net_fail = 0
            me._following = True
            me._poll()

        def failed(e) -> None:
            me = ref()
            if me is None or me._dead:
                return
            if e.status in (404, 409):
                answered()
                return
            if (e.status == 0 or e.status == 429 or e.status >= 500) and attempt < len(STOP_RETRY_S):
                _log(f"Stop not answered ({e.code}, HTTP {e.status}): sent again in {STOP_RETRY_S[attempt]:.0f} s")
                QTimer.singleShot(int(STOP_RETRY_S[attempt] * 1000), lambda: ref() is not None and ref()._send_stop(attempt + 1))
                return
            me._stop_sending = False
            me.stop_unreachable = True
            _log(f"Stop could not be delivered ({e.code}, HTTP {e.status}): TerraByte not reached")
            if not me._end_reported:
                me.problem_kind, me.problem_error = "network", e
                me._ended("failed", connection=True)
            me._fire_end_waiters(False)
        self._stop_call = self.client.stop_run(self.run_id, ok=answered, err=failed, timeout_s=15.0)

    def ensure_ended(self, ok, err) -> None:
        """Before the finished tiles are kept (Keep what finished) or the area run again, the run's end must be the
        service's: when only the plugin ended it (a lost connection, a Stop that did not get through) it may still be
        running there.  Stop it (until answered), follow it to its end and its charge, then ``ok()``; ``err()`` when
        TerraByte cannot be reached."""
        if self.end_confirmed and not self.charge_pending:
            ok()
            return
        self._end_waiters.append((ok, err))
        if self.end_confirmed:
            return
        self._stop_requested = True
        self.stop_unreachable = False
        self._send_stop()

    def _fire_end_waiters(self, reached: bool) -> None:
        waiters, self._end_waiters = self._end_waiters, []
        for ok, err in waiters:
            try:
                (ok if reached else err)()
            except Exception as e:
                _log(f"After the run's end: {type(e).__name__}: {e}")

    def _settle_charge(self) -> None:
        """The run ended stopped or failed and the service has not said what it charged (its tiles in flight may
        still finish): read the run's summary until the reservation is settled (``tiles_reserved`` 0), at most
        CHARGE_WAIT_S; then the figure is the service's, or unknown."""
        if self._dead or not self.charge_pending:
            return
        import weakref

        ref = weakref.ref(self)

        def again() -> None:
            me = ref()
            if me is None or me._dead:
                return
            if time.monotonic() >= me._charge_until:
                me._charge_settled()
                return
            QTimer.singleShot(1000, lambda: ref() is not None and ref()._settle_charge())

        def got(res) -> None:
            me = ref()
            if me is None or me._dead:
                return
            used, held = (res or {}).get("tiles_used"), (res or {}).get("tiles_reserved")
            if ((res or {}).get("state") in END_STATES and isinstance(used, (int, float)) and not isinstance(used, bool)
                    and not held):
                me.tiles_used = int(used)
                me._charge_settled()
                return
            again()
        self.client.get(f"/v1/runs/{self.run_id}", timeout_s=15.0, ok=got, err=lambda _e: again())

    def _charge_settled(self) -> None:
        if not self.charge_pending:
            return
        self.charge_pending = False
        try:
            self.changed.emit()
        except RuntimeError:
            return
        self._fire_end_waiters(True)

    def _set_going(self) -> None:
        """The run is set going (begin, Try again): nothing is known yet of how this attempt ends."""
        self._following = True
        self.end_confirmed = self.charge_pending = False
        self._end_phase, self._end_reads = None, 0
        self.stop_answered = self.stop_unreachable = False
        self._stop_sending = False
        self._attempts.clear()
        self._rate_waits.clear()

    def _start_run(self) -> None:
        if self._dead or self._stop_requested:
            return
        self.client.start_run(self.run_id, stream=self.streaming, ok=self._started, err=self._start_failed)

    def _started(self, res) -> None:
        self._ever_started = True
        self.state = "extracting"
        self.phase.emit("extracting")
        self.changed.emit()
        self._poll()
        if self.streaming and not self._stream_upload_begun and not (self._dead or self._stop_requested):
            self._stream_upload_begun = True
            spec = stream_spec(self.ctx)
            self._start_uploads(stream_order(self.area.tiles, self.area.rows, self.area.cols, spec))

    def _start_failed(self, err: ApiError) -> None:
        text = f"{err.code} {err.message}".lower()
        if self._stop_requested or "already finished" in text or "already_finished" in text:
            self._poll()
            return
        if self.streaming and err.code == "tiles_missing" and not self._stream_upload_begun:
            self.streaming = False
            _log("The service refused a streaming start (tiles_missing): uploading first, as before")
            self._start_uploads(list(sorted(self.area.tiles)))
            return
        if err.code == "busy" or err.code == "rate_limited":
            self.ctx.refresh_account()
        self._fail_request(err)

    def retry_start(self) -> None:
        """Server was busy: nothing ran; start the same run again (it reserves its tiles again).  With streaming
        nothing was uploaded yet: the start comes first again, then the upload."""
        if not self._allowed():
            return
        self._interrupted = True
        if self.run_id:
            self.state = "uploading"
            self.problem_kind = ""
            self._end_reported = False
            self._set_going()
            self.changed.emit()
            self._start_run()
        else:
            self.begin()

    def _poll(self) -> None:
        if self._polling or self._dead or not self._following:
            return
        self._polling = True
        self.client.events(self.run_id, self._after, self.wait_s, ok=self._events, err=self._events_failed)

    def _events(self, res) -> None:
        self._polling = False
        if not self._following:
            return
        self._net_fail = 0
        self._after = res.get("next_after", self._after)
        prog = res.get("progress") or {}
        self.elapsed_s = _num(prog.get("elapsed_s"), self.elapsed_s) or self.elapsed_s
        self.eta_s = _num(prog.get("eta_s"), None)
        for ev in res.get("events", []):
            self._apply(ev)
        st = res.get("state", self.state)
        if (st in END_STATES and self._end_phase is None and res.get("events") and self._end_reads < END_READ_TRIES):
            self._end_reads += 1
            QTimer.singleShot(self.poll_delay_ms, self._poll)
            return
        if st in END_STATES:
            self.end_confirmed = True
            self._ended(st)
            self._at_service_end(st)
            if not self.charge_pending:
                self._fire_end_waiters(True)
            return
        if st in ("extracting", "joining") and self.state != "extracting":
            self.state = "extracting"
        self.changed.emit()
        QTimer.singleShot(self.poll_delay_ms if res.get("events") else max(self.poll_delay_ms, 100), self._poll)

    def _apply(self, ev: Dict[str, Any]) -> None:
        t = ev.get("type")
        rc = (ev.get("row"), ev.get("col"))
        if t == "tile_started":
            self.started_tiles.add(rc)
        elif t == "tile_done":
            self.started_tiles.discard(rc)
            self.done_tiles.add(rc)
            if ev.get("no_imagery"):
                self.empty_tiles.add(rc)
            self.tile_lines[rc] = ev.get("lines") or []
            self.tile_done.emit(rc[0], rc[1], self.tile_lines[rc])
        elif t == "tile_failed":
            self.started_tiles.discard(rc)
            self.failed_tiles.add(rc)
        elif t == "phase":
            if ev.get("phase") == "extracting":
                self.state = "extracting"
            elif ev.get("phase") in END_STATES:
                self._end_phase = ev.get("phase")
            if "tiles_used" in ev:
                self.tiles_used = ev["tiles_used"]

    def _events_failed(self, err: ApiError) -> None:
        self._polling = False
        if self._dead:
            return
        waits = EVENTS_RETRY_S
        again = err.code == "network_error" or err.status >= 500 or err.status == 429
        if again:
            self._interrupted = True
        if again and self._net_fail < len(waits):
            _log(f"Run events: no answer ({err.code}, HTTP {err.status}: "
                 f"{(err.extra or {}).get('detail') or err.message}), asked again in {waits[self._net_fail]:g} s")
            QTimer.singleShot(int(waits[self._net_fail] * 1000), self._poll)
            self._net_fail += 1
            return
        if not again and (err.status == 404 or err.code != "network_error"):
            if self._end_waiters:
                self._fire_end_waiters(False)
            self._fail_request(err)
            return
        _log(f"Run events not reachable ({err.code}, HTTP {err.status}): "
             f"{(err.extra or {}).get('detail') or err.message}")
        if self.streaming and not self.stop_answered and not self.stop_unreachable and not self.end_confirmed:
            self._stop_requested = True
            self.upload_broken = self.upload_broken or ApiError(0, "network_error", err.message, err.extra)
            if self.uploading or self._reader is not None:
                self._halt_upload()
            self._net_fail = 0
            self._send_stop()
            return
        self.problem_kind, self.problem_error = "network", err
        self._ended("failed", connection=True)
        self._fire_end_waiters(False)

    def _ended(self, state: str, connection: bool = False) -> None:
        """The service says the run ended.  An end is reported once per start; a later report of the same end (the
        poll a stop reply starts after the end was read) updates the state but is not a second end: no second
        ``finished``."""
        again = self._end_reported
        self._end_reported = True
        if not again:
            self.ends += 1
            self._t_end = time.time()
        self.state = state
        settle = (not connection and state in ("stopped", "failed") and self.tiles_used is None and bool(self.run_id)
                  and not self.charge_pending and self.end_confirmed)
        if settle:
            self.charge_pending = True
            self._charge_until = time.monotonic() + CHARGE_WAIT_S
        if self.uploading or self._reader is not None:
            if connection and self.streaming and self.upload_broken is None:
                self.upload_broken = ApiError(0, "network_error", "The connection dropped during the upload.")
            self._halt_upload()
        self.connection_lost = connection
        if connection:
            self._following = False
        if not again:
            self._balance_settles()
        self.changed.emit()
        if not again:
            self.finished.emit(state)
        if settle:
            self._settle_charge()

    def record_time(self) -> None:
        """Feed the next "Takes" estimate with this run, measured from the Extract click to the moment its lines are on
        screen to keep (what the user waits for: the upload, the run, the result and the review)."""
        if self.state != "done" or not self.tiles_decoded or self._timed or self._stop_requested:
            return
        try:
            interrupted = bool(self._interrupted)
        except (AttributeError, RuntimeError):
            interrupted = False
        if interrupted:
            return
        self._timed = True
        from . import estimate

        took = time.time() - self._t0
        try:
            area, read_s, read_n = self.area, self.read_s, self.tiles_read
        except (AttributeError, RuntimeError):
            area, read_s, read_n = None, 0.0, 0
        extra = estimate.read_extra_of(area, read_s) if area is not None else 0.0
        if extra > 0 and read_n:
            estimate.record_read(self.ctx.settings, read_s, read_n * estimate.tile_msamples(area))
        estimate.record(self.ctx.settings, self.tiles_decoded, max(0.5, took - extra) if extra > 0 else took)

    def _balance_settles(self) -> None:
        """The run ended or was let go: the balance is read again now and while the service settles it."""
        left = getattr(self.ctx, "after_run_left", None)
        if callable(left):
            left()
        else:
            self.ctx.refresh_account()

    def stop(self) -> bool:
        """Ask the service to stop.  A no-op (False) when the run already ended: the caller says so."""
        if not self.running:
            return False
        if self._stop_requested:
            return self.upload_broken is not None
        self._stop_requested = True
        if self.checking:
            self._stop_reader()
            import weakref

            ref = weakref.ref(self)
            QTimer.singleShot(0, lambda: ref() is not None and ref()._check_stopped())
            return True
        if self.streaming and self.uploading:
            self._halt_upload()
        else:
            self._stop_reader()
            self._queue.clear()
        if not self.run_id:
            return True
        self._send_stop()
        return True

    def stop_quietly(self, wait_ms: int = 0):
        """Tell the service to stop this run, as the Stop button does, without a reply to wait for (the service
        then charges only the tiles that finish).  Used when the project changes, the raster is removed or QGIS quits.
        The upload halts at once (nothing more is encoded, the tiles in flight are aborted).  ``wait_ms`` (quit only):
        wait at most this long for the stop request, the one sent now or one sent earlier."""
        if self.state in END_STATES:
            return None
        call = None
        if self._stop_requested:
            call = self._stop_call
            self._halt_upload()
            self._join_readers()
        else:
            self._stop_requested = True
            self._halt_upload()
            self._join_readers()
            if not self.run_id:
                return None
            try:
                call = self._stop_call = self.client.stop_run(self.run_id, ok=lambda _r: None, err=lambda _e: None,
                                                              timeout_s=4.0)
            except Exception:
                return None
            if not wait_ms:
                self._balance_settles()
        if wait_ms and call is not None and not getattr(call, "finished", True):
            from qgis.PyQt.QtCore import QEventLoop

            loop = QEventLoop()
            call.done.connect(lambda *_: loop.quit())
            QTimer.singleShot(wait_ms, loop.quit)
            if not call.finished:
                loop.exec()
        return call

    def _at_service_end(self, state: str) -> None:
        """The service's end of this attempt was read (its phase event included): a done or stopped run's result is
        fetched now, before any dialog or click, and the service is told to delete it once fetched; any other end is
        acknowledged at once (the plugin holds the lines of a failed run; nothing_found has none)."""
        if not self.run_id:
            return
        if state in ("done", "stopped") and self.tiles_done:
            self.prefetch_result()
        else:
            acknowledge_result(self.ctx, self.run_id, state)

    def prefetch_result(self) -> None:
        """Fetch this end's result once; whoever needs it later gets it from ``result_for_end``.  Once it is answered
        (or refused), the service is told to delete its copy: the plugin has the result or the lines it kept."""
        end = self.ends
        if self._pre is not None and self._pre["end"] == end:
            return
        pre: Dict[str, Any] = {"end": end, "res": None, "err": None, "done": False, "waiters": []}
        self._pre = pre
        import weakref

        ref = weakref.ref(self)

        def answered(res, err) -> None:
            pre["res"], pre["err"], pre["done"] = res, err, True
            me = ref()
            if me is not None and not me._dead:
                acknowledge_result(me.ctx, me.run_id, me.state)
                me.changed.emit()
            waiters, pre["waiters"] = pre["waiters"], []
            for ok, bad in waiters:
                try:
                    ok(res) if err is None else bad(err)
                except Exception as e:
                    _log(f"After the run's result: {type(e).__name__}: {e}")
        self.fetch_result(lambda res: answered(res, None), lambda e: answered(None, e))

    def result_for_end(self, ok, err) -> None:
        """This end's result: the one fetched at the end (at once, or when it is answered), else fetched now."""
        pre = self._pre
        if pre is None or pre["end"] != self.ends:
            self.prefetch_result()
            pre = self._pre
        if pre["done"]:
            res, e = pre["res"], pre["err"]
            QTimer.singleShot(0, lambda: ok(res) if e is None else err(e))
        else:
            pre["waiters"].append((ok, err))

    def result_refused(self) -> bool:
        """True when the result fetched at this end could not be had (410, no answer): the review is then the lines
        held."""
        pre = self._pre
        return bool(pre is not None and pre["end"] == self.ends and pre["done"] and pre["err"] is not None)

    def read_status(self, ok, err) -> None:
        """One read of the run's state and charge (``GET /v1/runs/{id}``)."""
        self.client.get(f"/v1/runs/{self.run_id}", timeout_s=15.0, ok=ok, err=err)

    def follow_end(self, ok, err) -> None:
        """The service said the run ended while the plugin was cut off: read its events to that end (no Stop is
        sent), so the lines of tiles that finished meanwhile are kept while the service still has them."""
        if self.end_confirmed and not self.charge_pending:
            ok()
            return
        self._end_waiters.append((ok, err))
        if self.end_confirmed:
            return
        self._following, self._net_fail = True, 0
        self._poll()

    @property
    def tiles_held(self) -> int:
        """Finished tiles whose lines reached the plugin (tiles with no imagery have none and are not charged)."""
        return len(set(self.tile_lines) - self.empty_tiles)

    def held_result(self) -> Dict[str, Any]:
        """A result built from the lines the plugin kept from the ``tile_done`` events: numbered 1..N here, each
        with its tile, and no flagged tiles (the events carry none).  A line the events gave for two tiles is kept
        once."""
        lines: List[Dict[str, Any]] = []
        seen = set()
        for (r, c), tile in self.tile_lines.items():
            for ln in tile or []:
                pts = ln.get("points") or []
                key = (int(ln.get("class_id", 0) or 0), tuple(tuple(p) for p in pts))
                if len(pts) < 2 or key in seen:
                    continue
                seen.add(key)
                lines.append({"id": len(lines) + 1, "class_id": key[0], "confidence": ln.get("confidence"),
                              "points": pts, "tiles": [[r, c]]})
        used = self.tiles_used if self.tiles_used is not None else self.tiles_decoded
        summary = {"run_id": self.run_id, "tiles_used": used, "lines": len(lines), "tiles_held": self.tiles_held}
        return {"run_id": self.run_id, "state": self.state, "summary": summary, "lines": lines, "flagged_tiles": [],
                "client_ref": self.run_ref().to_client_ref()}

    def fetch_result(self, ok, err=None) -> None:
        def got(res):
            self.result = res
            self.summary = self.client_summary(res)
            used = (self.summary or {}).get("tiles_used")
            if isinstance(used, (int, float)) and not isinstance(used, bool):
                self.tiles_used = int(used)
            ok(res)
        self.client.result(self.run_id, ok=got, err=err or (lambda e: self._fail_request(e)))

    @staticmethod
    def client_summary(res: Dict[str, Any]) -> Dict[str, Any]:
        return res.get("summary") or {}

    def dispose(self) -> None:
        """Finished with this run: stop polling and let go of every listener (screens, overlay, flow wiring).  Its
        reader stops and is waited for (at most READER_JOIN_S: it stops between pieces)."""
        self._dead = True
        self._end_waiters = []
        self.charge_pending = False
        try:
            self._stop_reader(wait=True)
        except RuntimeError:
            pass
        for sig in (self.changed, self.tile_done, self.phase, self.finished, self.problem):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
