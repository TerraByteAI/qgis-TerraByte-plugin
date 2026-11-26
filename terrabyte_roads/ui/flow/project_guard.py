"""Keeps the temporary review layers out of reopened projects, and ends the review when the project goes away.

The plugin never touches the project while it is being saved (no hooks on any save signal): the review group and its
layers are tagged with a custom property, and a project that carries them when it is READ (or when the plugin loads)
loses them.  The window's close button asks the "Leave without exporting?" question; Ctrl+Q, New, Open and Close show
QGIS's own prompt and the review then ends cleanly when the project clears.
"""
from __future__ import annotations

from typing import List

from qgis.core import Qgis, QgsProject
from qgis.PyQt.QtCore import QObject, QTimer

GROUP_NAME = "TerraByte review"
PROP = "terrabyte_roads/review"
REVIEW_BOARDS = ("RV-Keep", "RV-Export")
RAN_ONLY = "Only finished tiles are charged."
CHARGE_POLLS = 4

ON_RASTER_GONE = {
    "EN-SignedOut": "ignore", "EN-SignInWait": "ignore", "EN-Update": "ignore",
    "EX-NoImagery": "follow", "EX-Start": "follow",
    "EX-DrawArea": "leave", "EX-Setup": "leave", "EX-OverBudget": "leave",
    "EX-Running": "stop", "EX-Failed": "card",
    "RV-Keep": "keep", "RV-Export": "keep", "RV-Recap": "recap",
}


def review_layer_ids(project=None) -> List[str]:
    prj = project or QgsProject.instance()
    return [l.id() for l in prj.mapLayers().values() if l.customProperty(PROP)]


def _forget_in_snapping(prj, ids) -> None:
    """A snapping set-up read from an old file may still name a removed layer; drop those entries so a later save
    never meets a dangling reference."""
    try:
        cfg = prj.snappingConfig()
        gone = [l for l in list(cfg.individualLayerSettings().keys()) if l is None or l.id() in ids]
        if not gone:
            return
        for l in gone:
            cfg.removeLayers([l]) if hasattr(cfg, "removeLayers") else None
        prj.setSnappingConfig(cfg)
    except Exception:
        pass


def remove_stale(project=None) -> int:
    """Remove the plugin's review layers and "TerraByte review" group from a project that was just read (or that
    was open when the plugin loaded).  Layers first, through the project, then the group.  Never call this from
    inside a ``readProject`` handler: it runs a turn later."""
    prj = project or QgsProject.instance()
    root = prj.layerTreeRoot()
    n = 0
    ids = set(review_layer_ids(prj))
    for g in root.findGroups():
        if g.name() == GROUP_NAME or g.customProperty(PROP):
            ids.update(g.findLayerIds())
    for l in prj.mapLayers().values():
        if l.providerType() == "memory" and l.name().endswith("(review)") and l.customProperty(PROP) is None:
            ids.add(l.id())
    ids = sorted(ids)
    if ids:
        _forget_in_snapping(prj, set(ids))
        prj.removeMapLayers(ids)
        n += len(ids)
    for g in list(root.findGroups()):
        if g.name() == GROUP_NAME or g.customProperty(PROP):
            root.removeChildNode(g)
            n += 1
    return n


def report_charge(ctx, run_id: str, after: int = 0, tries: int = CHARGE_POLLS, delay_ms: int = 1500) -> None:
    """A run let go by a project change was asked to stop: once the service ends it, say in one bar line what it charged
    for it (the final phase event's ``tiles_used``).  A few long polls on the event loop; nothing waits for them."""
    import weakref

    from ...cloud.grid import tile_cost_label
    from ...cloud.runner import END_STATES

    if not run_id or tries <= 0:
        return
    ref = weakref.ref(ctx)

    def got(res):
        c = ref()
        if c is None or getattr(c, "shut_down", False):
            return
        used = None
        for ev in (res or {}).get("events") or []:
            if ev.get("type") == "phase" and ev.get("phase") in END_STATES and "tiles_used" in ev:
                used = ev.get("tiles_used")
        if used is None or (res or {}).get("state") not in END_STATES:
            report_charge(c, run_id, (res or {}).get("next_after", after), tries - 1, delay_ms)
            return
        try:
            n = int(used)
            text = f"Stopped run: TerraByte charged {tile_cost_label(n)}." if n else "Stopped run: TerraByte charged no tiles."
            c.iface.messageBar().pushMessage("Road Extractor", text, Qgis.MessageLevel.Info, 10)
        except Exception:
            pass

    def ask():
        c = ref()
        if c is None or getattr(c, "shut_down", False) or getattr(c, "client", None) is None:
            return
        try:
            c.client.events(run_id, after, 10, ok=got, err=lambda _e: None)
        except Exception:
            pass
    QTimer.singleShot(delay_ms, ask)


class ProjectGuard(QObject):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx = ctx
        prj = QgsProject.instance()
        prj.readProject.connect(self._on_read)
        prj.aboutToBeCleared.connect(self._on_clearing)
        prj.layersRemoved.connect(self._on_layers_removed)
        self._reset_pending = False

    def dispose(self) -> None:
        prj = QgsProject.instance()
        for sig, slot in ((prj.readProject, self._on_read), (prj.aboutToBeCleared, self._on_clearing),
                          (prj.layersRemoved, self._on_layers_removed)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_read(self, *_a) -> None:
        QTimer.singleShot(0, self._clean)
        self._schedule_reset()

    def _clean(self) -> None:
        if getattr(self.ctx, "review", None) is not None:
            return
        try:
            if remove_stale():
                self.ctx.iface.messageBar().pushMessage(
                    "Road Extractor", "The lines of an unfinished review were not kept.", Qgis.MessageLevel.Info, 8)
        except Exception:
            pass

    def _schedule_reset(self) -> None:
        if self._reset_pending:
            return
        self._reset_pending = True
        QTimer.singleShot(0, self._reset)

    def _on_layers_removed(self, *_a) -> None:
        """A raster the flow refers to left the project (Layers panel, or a project change)."""
        try:
            ctx = self.ctx
            i = getattr(ctx, "imagery", None)
            if i is None or ON_RASTER_GONE.get(ctx.current_board) not in ("leave", "recap", "card", "stop"):
                return
            if QgsProject.instance().mapLayer(getattr(i, "layer_id", "") or "") is None:
                if ON_RASTER_GONE.get(ctx.current_board) == "stop" and getattr(ctx, "run", None) is not None:
                    ctx.run.stop()
                    ctx.run.release_file()
                QTimer.singleShot(0, self._imagery_gone)
        except Exception:
            pass

    def _imagery_gone(self) -> None:
        try:
            ctx = self.ctx
            i = getattr(ctx, "imagery", None)
            if i is None or QgsProject.instance().mapLayer(getattr(i, "layer_id", "") or "") is not None:
                return
            from . import extract

            how = ON_RASTER_GONE.get(ctx.current_board)
            if how == "stop":
                run = getattr(ctx, "run", None)
                if run is not None:
                    ctx.iface.messageBar().pushMessage(
                        "Road Extractor", f"Imagery removed: run stopped. {RAN_ONLY}", Qgis.MessageLevel.Warning, 10)
                return
            if how == "recap":
                ctx.imagery = ctx.area = None
                return
            if how == "card":
                s = ctx.panel.screen("EX-Failed") if ctx.panel is not None else None
                if s is not None and getattr(s, "shown_kind", "") in ("connection", "stopped"):
                    s.refresh()
                    return
            extract.refuse_stale_imagery(ctx)
        except Exception:
            pass

    def _reset(self) -> None:
        """The project was cleared or another was read: drop every reference to the old project's layers and show the
        entry screen of the new one.  A run in progress is let go with a plain note."""
        self._reset_pending = False
        ctx = self.ctx
        try:
            if getattr(ctx, "review", None) is not None:
                return
            note = ""
            run = getattr(ctx, "run", None)
            if run is not None:
                try:
                    if run.running:
                        run.stop_quietly()
                        note = f"Run stopped: the project changed. {RAN_ONLY}"
                        report_charge(ctx, run.run_id, getattr(run, "_after", 0))
                    run.dispose()
                except Exception:
                    pass
                ctx.run = None
            ctx.imagery = ctx.area = None
            for k in ("last_export", "layer_id", "imagery_check"):
                ctx.extras.pop(k, None)
            from . import extract

            extract.clear_overlay(ctx)
            board = ctx.current_board
            if board.startswith(("EX-", "RV-")) and getattr(ctx, "signed_in", False):
                ctx.close_dialog("RV-Exit")
                ctx.go("EX-Start")
            if note:
                ctx.iface.messageBar().pushMessage("Road Extractor", note, Qgis.MessageLevel.Warning, 12)
        except Exception:
            pass

    def _on_clearing(self) -> None:
        """New / open / close project: the review's layers go with the project, so end the session."""
        ctx = self.ctx
        run = getattr(ctx, "run", None)
        if run is not None:
            try:
                if run.running:
                    run.stop_quietly()
                    from . import extract

                    extract.clear_overlay(ctx)
            except Exception:
                pass
        self._schedule_reset()
        if getattr(ctx, "review", None) is None:
            return
        from .review import teardown

        try:
            teardown(ctx)
        except Exception:
            ctx.review = None
        QTimer.singleShot(0, self._to_start)

    def _to_start(self) -> None:
        try:
            ctx = self.ctx
            if ctx.current_board.startswith("RV-") or ctx.dialogs.get("RV-Exit") is not None:
                ctx.close_dialog("RV-Exit")
                ctx.go("EX-Start")
        except Exception:
            pass

