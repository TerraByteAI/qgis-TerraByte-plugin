"""Resumable, verified HTTP downloads (no QGIS imports; usable from worker threads).

Why urllib and not QgsNetworkAccessManager / QNetworkAccessManager: these are large
streamed files fetched from a QgsTask worker thread; QGIS's network manager is bound to
the main thread's event loop. We instead derive a ``ProxyHandler`` from the QGIS proxy
settings (see ``qgis_proxy_url`` below).
"""
from __future__ import annotations

import errno
import http.client
import hashlib
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from .types import CancelCheck, ProgressCallback, TaskCancelled

CHUNK = 256 * 1024
TIMEOUT_S = 30
MAX_RETRIES = 4
_BACKOFF_BASE = 2.0
_RETRY_HTTP = {408, 425, 429, 500, 502, 503, 504}


class DownloadError(RuntimeError):
    """Permanent download failure (message is user-readable)."""


def user_agent() -> str:
    try:
        from .version import plugin_version

        v = plugin_version()
    except Exception:
        v = "0.0.0"
    return f"TerraByteRoads-QGIS/{v}"


_MAC_CA_BUNDLES = ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem")


def ssl_context(platform: Optional[str] = None) -> ssl.SSLContext:
    """TLS context using the OS trust store where Python can (Windows: the system cert store is
    loaded by ``ssl.create_default_context``). Some macOS Python builds ship without any CA
    certificates; then fall back to ``certifi`` or the system bundle ``/etc/ssl/cert.pem``."""
    plat = platform or sys.platform
    ctx = ssl.create_default_context()
    if plat == "darwin":
        try:
            stats = ctx.cert_store_stats()
            have = stats.get("x509_ca", 0) + stats.get("x509", 0)
        except Exception:
            have = 1
        if have == 0:
            try:
                import certifi

                ctx.load_verify_locations(cafile=certifi.where())
                return ctx
            except Exception:
                pass
            for bundle in _MAC_CA_BUNDLES:
                if os.path.isfile(bundle):
                    try:
                        ctx.load_verify_locations(cafile=bundle)
                        break
                    except (ssl.SSLError, OSError):
                        continue
    return ctx


def build_opener(proxy_url: Optional[str] = None) -> urllib.request.OpenerDirector:
    handlers = [urllib.request.HTTPSHandler(context=ssl_context())]
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener(*handlers)


def replace_with_retry(src: Any, dst: Any, attempts: int = 10, delay: float = 0.3) -> None:
    """``os.replace`` that tolerates the transient sharing violations Windows raises while an
    antivirus scanner or indexer still holds the freshly written file (PermissionError)."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (1 + i / 2))


def proxy_url_from_settings(s: Mapping[str, Any]) -> Optional[str]:
    """Build ``http://user:pw@host:port`` from QGIS ``proxy/*`` settings, or None.

    Returns None when the proxy is disabled, is the system/default proxy (the OS and
    environment variables apply then) or is SOCKS5 (not supported by urllib here)."""
    enabled = str(s.get("proxyEnabled", "")).lower() in ("true", "1")
    ptype = str(s.get("proxyType", "DefaultProxy"))
    host = str(s.get("proxyHost", "")).strip()
    if not enabled or ptype in ("DefaultProxy", "Socks5Proxy") or not host:
        return None
    port = str(s.get("proxyPort", "")).strip()
    user = str(s.get("proxyUser", "")).strip()
    pw = str(s.get("proxyPassword", ""))
    auth = ""
    if user:
        auth = quote(user, safe="") + (":" + quote(pw, safe="") if pw else "") + "@"
    return f"http://{auth}{host}" + (f":{port}" if port else "")


def qgis_proxy_url() -> Optional[str]:
    """Proxy URL from the running QGIS's settings (None outside QGIS or when disabled)."""
    try:
        from qgis.core import QgsSettings

        st = QgsSettings()
        keys = ("proxyEnabled", "proxyType", "proxyHost", "proxyPort", "proxyUser", "proxyPassword")
        return proxy_url_from_settings({k: st.value("proxy/" + k, "") for k in keys})
    except Exception:
        return None


def default_opener() -> urllib.request.OpenerDirector:
    return build_opener(qgis_proxy_url())


def fetch_json(url: str, opener: Optional[urllib.request.OpenerDirector] = None, timeout: int = TIMEOUT_S) -> Any:
    op = opener or default_opener()
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": "application/json"})
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sha256_file(path: Path, is_cancelled: Optional[CancelCheck] = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            if is_cancelled and is_cancelled():
                raise TaskCancelled()
    return h.hexdigest()


def _sleep(seconds: float, is_cancelled: Optional[CancelCheck]) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if is_cancelled and is_cancelled():
            raise TaskCancelled()
        time.sleep(min(0.1, max(0.0, end - time.monotonic())))


class _Transient(Exception):
    pass


def _no_query(url: str) -> str:
    """URL without query string/fragment: a signed URL's signature must never reach messages or logs."""
    return url.split("?", 1)[0].split("#", 1)[0]


def _classify_http(e: urllib.error.HTTPError, url: str) -> Exception:
    if e.code in _RETRY_HTTP:
        return _Transient(f"HTTP {e.code}")
    return DownloadError(f"Server answered HTTP {e.code} {e.reason} for {_no_query(url)}")


def _attempt(url: str, part: Path, total_hint: Optional[int], opener, label: str,
             progress: Optional[ProgressCallback], is_cancelled: Optional[CancelCheck],
             ) -> None:
    """One streaming attempt, appending to ``part``. Raises _Transient / DownloadError."""
    offset = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "identity"}
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = opener.open(req, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as e:
        if e.code == 416 and offset > 0:
            cr = e.headers.get("Content-Range", "")
            if cr.strip() == f"bytes */{offset}":
                return
            part.unlink(missing_ok=True)
            raise _Transient("range not satisfiable; restarting")
        raise _classify_http(e, url)
    except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
        raise _Transient(f"network error: {getattr(e, 'reason', e)}")

    with resp:
        status = getattr(resp, "status", 200)
        mode = "ab"
        if offset > 0:
            if status == 206:
                m = re.match(r"bytes (\d+)-", resp.headers.get("Content-Range", ""))
                if not m or int(m.group(1)) != offset:
                    mode, offset = "wb", 0
            else:
                mode, offset = "wb", 0
        clen = resp.headers.get("Content-Length")
        total = offset + int(clen) if clen and clen.isdigit() else total_hint
        done = offset
        t0 = time.monotonic()
        last = 0.0
        start_done = done
        try:
            with open(part, mode) as f:
                while True:
                    if is_cancelled and is_cancelled():
                        raise TaskCancelled()
                    try:
                        chunk = resp.read(CHUNK)
                    except (OSError, http.client.HTTPException) as e:
                        raise _Transient(f"connection lost: {e}")
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if progress and now - last >= 0.25:
                        last = now
                        speed = (done - start_done) / max(now - t0, 1e-6) / 1e6
                        mb = done / 1e6
                        if total:
                            progress(min(done / total, 1.0),
                                     f"{label}: {mb:.1f} / {total / 1e6:.1f} MB ({speed:.1f} MB/s)")
                        else:
                            progress(0.0, f"{label}: {mb:.1f} MB ({speed:.1f} MB/s)")
        except OSError as e:
            if e.errno == errno.ENOSPC:
                raise DownloadError("The disk is full while downloading; free some space and retry.")
            raise
    if total is not None and done != total:
        raise _Transient(f"incomplete download ({done} of {total} bytes)")


def download_file(url: str, dest: Path, *, sha256: Optional[str] = None, size: Optional[int] = None,
                  label: Optional[str] = None, progress: Optional[ProgressCallback] = None,
                  is_cancelled: Optional[CancelCheck] = None, opener=None,
                  on_tick: Optional[Callable[[], None]] = None) -> Path:
    """Download ``url`` to ``dest`` (atomically), resuming ``dest.part`` via HTTP Range.

    Verifies ``sha256`` when given; a mismatch deletes the file and retries once from
    scratch before raising DownloadError. Transient errors retry with exponential backoff.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    label = label or dest.name
    opener = opener or default_opener()
    want = sha256.lower() if sha256 else None

    def tick(frac: float, msg: str) -> None:
        if on_tick:
            on_tick()
        if progress:
            progress(frac, msg)

    for verify_try in range(2):
        attempt = 0
        while True:
            try:
                _attempt(url, part, size, opener, label, tick, is_cancelled)
                break
            except _Transient as e:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise DownloadError(
                        f"Download of {label} failed after {MAX_RETRIES} retries ({e}). Check your internet "
                        f"connection/proxy and try again; the partial file is kept and will be resumed.")
                wait = min(_BACKOFF_BASE * 2 ** (attempt - 1), 30.0)
                tick(0.0, f"{label}: {e}; retrying in {wait:.0f}s ({attempt}/{MAX_RETRIES})")
                _sleep(wait, is_cancelled)
        if want is None:
            break
        tick(1.0, f"{label}: verifying checksum")
        got = sha256_file(part, is_cancelled)
        if got == want:
            break
        part.unlink(missing_ok=True)
        if verify_try == 1:
            raise DownloadError(
                f"Checksum mismatch for {label} (expected {want[:12]}..., got {got[:12]}...). "
                f"The download is corrupt or the source file changed.")
        tick(0.0, f"{label}: checksum mismatch, downloading again")
    replace_with_retry(part, dest)
    return dest


class DirLock:
    """Cross-process lock file (O_EXCL create). Stale after ``stale_s`` without ``touch()``."""

    def __init__(self, path: Path, stale_s: float = 120.0):
        self.path = Path(path)
        self.stale_s = stale_s
        self._held = False

    def try_acquire(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > self.stale_s:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                return False
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {time.time()}\n")
            self._held = True
            return True
        return False

    def acquire(self, wait_s: float, is_cancelled: Optional[CancelCheck] = None) -> bool:
        end = time.monotonic() + wait_s
        while True:
            if self.try_acquire():
                return True
            if time.monotonic() >= end:
                return False
            _sleep(0.2, is_cancelled)

    def touch(self) -> None:
        if self._held:
            try:
                os.utime(self.path, None)
            except OSError:
                pass

    def release(self) -> None:
        if self._held:
            self._held = False
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "DirLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()

