"""Asynchronous client for the Road Extractor cloud API.

All HTTP is asynchronous on the QGIS network access manager (``QgsNetworkAccessManager``: proxy, authentication
and SSL settings apply).  Replies finish through Qt signals, so the QGIS main thread is never blocked and no
worker threads are involved.  Results arrive through ``Call`` objects:

    call = client.get("/v1/me")
    call.done.connect(lambda result, err: ...)      # exactly one of result / err is not None
    # or
    client.get("/v1/me", ok=handle, err=handle_err)

``ApiError`` carries the service's error body (status, code, message, extra fields).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin

from qgis.PyQt.QtCore import QByteArray, QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest

DEFAULT_BASE_URL = "https://plugin.terrabyte.ai"
ENV_BASE_URL = "TERRABYTE_ROADS_CLOUD_URL"
SETTING_BASE_URL = "cloud/base_url"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}

    def __repr__(self) -> str:
        return f"ApiError({self.status}, {self.code!r}, {self.message!r})"


def resolve_base_url(settings=None) -> str:
    """Env var wins over the ``cloud/base_url`` setting; falls back to the live service."""
    env = os.environ.get(ENV_BASE_URL, "").strip()
    if env:
        return env.rstrip("/")
    if settings is not None:
        try:
            v = str(settings._s.value(settings.PREFIX + SETTING_BASE_URL, "") or "").strip()
        except Exception:
            v = ""
        if v:
            return v.rstrip("/")
    return DEFAULT_BASE_URL


class Call(QObject):
    """One in-flight request.  ``done(result, error)`` fires once on the thread that created it."""

    done = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self.result = None
        self.error: Optional[ApiError] = None
        self.finished = False
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        r = getattr(self, "_reply", None)
        if r is not None and not self.finished:
            try:
                r.abort()
            except RuntimeError:
                pass

    def then(self, ok: Optional[Callable] = None, err: Optional[Callable] = None) -> "Call":
        def _fire(result, error):
            if error is not None:
                if err is not None:
                    err(error)
            elif ok is not None:
                ok(result)
        self.done.connect(_fire)
        return self


class CloudClient(QObject):
    """Thin REST client.  One instance per plugin; ``base_url`` overrides ``resolve_base_url`` and ``token_getter``
    supplies the bearer token (none by default)."""

    session_expired = pyqtSignal()
    update_required = pyqtSignal(dict)
    reached = pyqtSignal(str)

    def __init__(self, base_url: Optional[str] = None, token_getter: Optional[Callable[[], str]] = None,
                 plugin_version: str = "0.0.0", settings=None, parent=None):
        super().__init__(parent)
        self._base = base_url
        self._settings = settings
        self._token_getter = token_getter or (lambda: "")
        self.plugin_version = plugin_version
        self._live: set = set()

    @property
    def base_url(self) -> str:
        return (self._base or resolve_base_url(self._settings)).rstrip("/")

    def set_base_url(self, url: Optional[str]) -> None:
        self._base = url

    def request(self, method: str, path: str, json_body: Any = None, *, data: Optional[bytes] = None,
                content_type: str = "application/json", auth: bool = True, timeout_s: float = 40.0,
                ok: Optional[Callable] = None, err: Optional[Callable] = None) -> Call:
        from qgis.core import QgsNetworkAccessManager

        call = Call()
        if ok is not None or err is not None:
            call.then(ok, err)
        req = QNetworkRequest(QUrl(self.base_url + path))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, content_type)
        req.setRawHeader(b"X-Plugin-Version", self.plugin_version.encode())
        req.setRawHeader(b"Accept", b"application/json, image/png")
        token = self._token_getter() if auth else ""
        if token:
            req.setRawHeader(b"Authorization", f"Bearer {token}".encode())
        try:
            req.setTransferTimeout(int(timeout_s * 1000))
        except Exception:
            pass
        body = data if data is not None else (json.dumps(json_body).encode() if json_body is not None else None)
        nam = QgsNetworkAccessManager.instance()
        m = method.upper()
        reply = nam.get(req) if m == "GET" else nam.sendCustomRequest(req, m.encode(), QByteArray(body or b""))
        call._reply = reply
        call._path = path
        self._live.add(call)
        reply.finished.connect(lambda c=call, r=reply: self._on_finished(c, r))
        return call

    def _on_finished(self, call: Call, reply) -> None:
        self._live.discard(call)
        try:
            res, err = _parse_reply(reply), None
        except ApiError as e:
            res, err = None, e
        except Exception as e:
            res, err = None, ApiError(0, "network_error", NETWORK_TEXT, {"detail": str(e) or type(e).__name__})
        try:
            reply.deleteLater()
        except RuntimeError:
            pass
        call.result, call.error, call.finished = res, err, True
        if err is not None and not getattr(call, "_quiet", False):
            if err.status == 401 and err.code == "not_signed_in":
                self.session_expired.emit()
            elif err.status == 426:
                self.update_required.emit(err.extra)
        if not call.cancelled:
            call.done.emit(res, err)
            if err is None or err.status:
                self.reached.emit(str(getattr(call, "_path", "") or ""))

    def get(self, path, **kw) -> Call:
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw) -> Call:
        return self.request("POST", path, body if body is not None else {}, **kw)

    def patch(self, path, body=None, **kw) -> Call:
        return self.request("PATCH", path, body if body is not None else {}, **kw)

    def delete(self, path, body=None, **kw) -> Call:
        return self.request("DELETE", path, body, **kw)

    def put_bytes(self, path, data: bytes, content_type="image/png", **kw) -> Call:
        return self.request("PUT", path, data=data, content_type=content_type, **kw)

    def meta(self, **kw): return self.get("/v1/meta", auth=False, **kw)
    def auth_device(self, **kw): return self.post("/v1/auth/device", {"client": "qgis-plugin", "plugin_version": self.plugin_version}, auth=False, **kw)
    def auth_token(self, device_code, user_code=None, **kw):
        body = {"device_code": device_code}
        if user_code:
            body["user_code"] = user_code
        return self.post("/v1/auth/token", body, auth=False, **kw)
    def signout(self, **kw): return self.post("/v1/auth/signout", {}, **kw)
    def me(self, **kw): return self.get("/v1/me", **kw)
    def consent(self, terms_version, **kw): return self.post("/v1/me/consent", {"terms_version": terms_version}, **kw)
    def create_run(self, body, **kw): return self.post("/v1/runs", without_local_names(body), timeout_s=15.0, **kw)
    def upload_tile(self, run_id, row, col, png: bytes, **kw): return self.put_bytes(f"/v1/runs/{run_id}/tiles/{row}_{col}", png, **kw)
    def start_run(self, run_id, stream: bool = False, **kw):
        """Start the run.  With ``stream`` the run starts before its tiles are uploaded; send it only to a service
        that announces ``meta.stream`` (without it the body is ``{}``)."""
        return self.post(f"/v1/runs/{run_id}/start", {"stream": True} if stream else {}, timeout_s=15.0, **kw)

    def events(self, run_id, after=0, wait_s=20, **kw): return self.get(f"/v1/runs/{run_id}/events?after={after}&wait_s={wait_s}", timeout_s=wait_s + 8, **kw)
    def stop_run(self, run_id, **kw): return self.post(f"/v1/runs/{run_id}/stop", {}, **kw)
    def result(self, run_id, **kw): return self.get(f"/v1/runs/{run_id}/result", timeout_s=120, **kw)

    def delete_result(self, run_id, **kw) -> Call:
        """Ask the service to delete its copy of the run's result once the plugin has the lines.  Quiet: its 401
        never signs the user out and its 426 never raises the update gate."""
        call = self.request("DELETE", f"/v1/runs/{run_id}/result", None, timeout_s=15.0, **kw)
        call._quiet = True
        return call


_LOCAL_REF_KEYS = ("layer_source", "layer_id", "layer_name")


def without_local_names(body: Any) -> Any:
    """The run-create body without ``imagery.name`` (the QGIS layer name, often the file's base name) and without the
    layer keys ``layer_source``, ``layer_id`` and ``layer_name`` in ``client_ref.opaque``.  Everything else is sent as
    given."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    img = out.get("imagery")
    if isinstance(img, dict) and "name" in img:
        out["imagery"] = {k: v for k, v in img.items() if k != "name"}
    ref = out.get("client_ref")
    if isinstance(ref, dict) and isinstance(ref.get("opaque"), dict) and any(k in ref["opaque"] for k in _LOCAL_REF_KEYS):
        out["client_ref"] = dict(ref, opaque={k: v for k, v in ref["opaque"].items() if k not in _LOCAL_REF_KEYS})
    return out


NETWORK_TEXT = "Can't reach TerraByte. Check your connection and try again."
SERVICE_TEXT = "TerraByte had a problem. Try again in a few minutes."


def _log(msg: str) -> None:
    """The raw text behind ``SERVICE_TEXT``: the QGIS message log (Road Extractor tab) and the plugin's log buffer."""
    try:
        from qgis.core import Qgis, QgsMessageLog

        from ..core import logbuffer

        logbuffer.record(msg)
        QgsMessageLog.logMessage(str(msg), "Road Extractor", Qgis.MessageLevel.Warning)
    except Exception:
        pass


def _parse_reply(reply) -> Any:
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    status = int(status) if status is not None else 0
    raw = bytes(reply.readAll())
    ctype = bytes(reply.rawHeader(b"Content-Type")).decode("utf-8", "ignore")
    if status == 0:
        raise ApiError(0, "network_error", NETWORK_TEXT, {"detail": reply.errorString() or ""})
    if 200 <= status < 300:
        if status == 204 or not raw:
            return {}
        if "json" in ctype or raw[:1] in (b"{", b"["):
            return json.loads(raw.decode("utf-8"))
        return raw
    try:
        e = json.loads(raw.decode("utf-8")).get("error", {})
    except Exception:
        e = {}
    extra = {k: v for k, v in e.items() if k not in ("code", "message")}
    msg = e.get("message")
    if not msg:
        msg = SERVICE_TEXT
        extra.setdefault("detail", f"HTTP {status}: {raw[:200].decode('utf-8', 'replace')}")
        _log(f"TerraByte answered without a message: {extra['detail']}")
    raise ApiError(status, e.get("code") or f"http_{status}", msg, extra)
