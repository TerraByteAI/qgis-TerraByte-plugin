"""Minimal STAC client for the Microsoft Planetary Computer (stdlib only, no QGIS).

* ``search`` / ``get_item`` talk to the STAC API (anonymous, read-only).
* ``sign_href`` appends a short-lived SAS token fetched from the PC token endpoint
  (cached per collection, refreshed ~5 min before expiry). Each user's QGIS fetches
  its own token; tokens must never be shared, logged or returned to clients.

Security rule: a signed URL or token never appears in return values, exceptions or
logs produced here. ``redact`` strips query strings from any text.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .version import plugin_version

STAC_ROOT = "https://planetarycomputer.microsoft.com/api/stac/v1"
SAS_ROOT = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
DEFAULT_COLLECTION = "naip"
TIMEOUT_S = 20.0
TOKEN_REFRESH_MARGIN_S = 300.0

PROVIDER = "Microsoft Planetary Computer"
ATTRIBUTION = "USDA Farm Service Agency NAIP, via Microsoft Planetary Computer"
TERMS_NOTE = ("NAIP is USDA FSA imagery, generally public domain (verify before redistribution). "
              "Planetary Computer access tokens are per user and must not be shared.")

_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ITEM_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_URL_QUERY_RE = re.compile(r"(https?://[^\s'\"<>?]+)\?[^\s'\"<>]*")


class StacError(Exception):
    """Network/HTTP/format failure. ``status`` is the HTTP status when known."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def user_agent() -> str:
    return f"TerraByteRoads-QGIS/{plugin_version()}"


def redact(text: Any) -> str:
    """Remove query strings (SAS tokens) from URLs inside ``text``."""
    return _URL_QUERY_RE.sub(r"\1", str(text))


def _now() -> float:
    return time.time()


RETRY_STATUSES = (429, 502, 503, 504)
RETRY_ATTEMPTS = 3
MAX_RETRY_WAIT_S = 8.0


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retry_delay(retry_after: Optional[str], attempt: int) -> float:
    try:
        wait = float(retry_after) if retry_after else 2.0 * (2 ** attempt)
    except ValueError:
        wait = 2.0 * (2 ** attempt)
    return max(0.0, min(wait, MAX_RETRY_WAIT_S))


def _request_json(url: str, body: Optional[dict] = None, timeout: float = TIMEOUT_S) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"User-Agent": user_agent(), "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    what = redact(url).replace("https://planetarycomputer.microsoft.com", "")
    raw = b""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUSES and attempt + 1 < RETRY_ATTEMPTS:
                _sleep(_retry_delay(e.headers.get("Retry-After") if e.headers else None, attempt))
                continue
            if e.code == 429:
                raise StacError(f"Planetary Computer is rate-limiting requests (HTTP 429) for {what}; "
                                "wait a minute and try again", e.code) from None
            raise StacError(f"HTTP {e.code} {str(e.reason or '')[:80]} for {what}", e.code) from None
        except urllib.error.URLError as e:
            raise StacError(f"network error for {what}: {redact(e.reason)[:120]}") from None
        except OSError as e:
            raise StacError(f"network error for {what}: {redact(e)[:120]}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise StacError(f"invalid JSON response for {what}") from None


def check_collection(collection: str) -> str:
    if not isinstance(collection, str) or not _COLLECTION_RE.match(collection):
        raise ValueError(f"invalid collection id {collection!r}")
    return collection


def bbox_coverage_pct(item_bbox: Sequence[float], query_bbox: Sequence[float]) -> float:
    """Percent of ``query_bbox`` area covered by ``item_bbox`` (both lon/lat, planar)."""
    ix = min(item_bbox[2], query_bbox[2]) - max(item_bbox[0], query_bbox[0])
    iy = min(item_bbox[3], query_bbox[3]) - max(item_bbox[1], query_bbox[1])
    area = (query_bbox[2] - query_bbox[0]) * (query_bbox[3] - query_bbox[1])
    if ix <= 0 or iy <= 0 or area <= 0:
        return 0.0
    return round(min(100.0, 100.0 * ix * iy / area), 1)


def item_epsg(props: Dict[str, Any]) -> Optional[int]:
    v = props.get("proj:epsg")
    if isinstance(v, int):
        return v
    code = props.get("proj:code")
    if isinstance(code, str) and code.upper().startswith("EPSG:") and code[5:].isdigit():
        return int(code[5:])
    return None


def compact_item(item: Dict[str, Any], query_bbox: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """Compact, token-free summary of a STAC item."""
    p = item.get("properties") or {}
    dt = p.get("datetime") or p.get("start_datetime")
    bbox = [round(float(v), 6) for v in (item.get("bbox") or [])[:4]]
    out: Dict[str, Any] = {
        "id": item.get("id"),
        "datetime": dt,
        "gsd_m": p.get("gsd"),
        "bbox": bbox,
        "epsg": item_epsg(p),
        "coverage_pct": bbox_coverage_pct(bbox, query_bbox) if (query_bbox and len(bbox) == 4) else None,
    }
    if p.get("naip:state"):
        out["state"] = p["naip:state"]
    year = p.get("naip:year") or (dt[:4] if isinstance(dt, str) and dt[:4].isdigit() else None)
    if year:
        out["year"] = int(year)
    return out


def asset_href(item: Dict[str, Any], key: str = "image") -> str:
    """Unsigned href of an asset (query string stripped). Raises StacError if missing."""
    assets = item.get("assets") or {}
    a = assets.get(key)
    if not a or not a.get("href"):
        raise StacError(f"item {item.get('id')!r} has no asset {key!r} (available: {sorted(assets)[:8]})")
    return str(a["href"]).split("?", 1)[0]


def _datetime_interval(start_date: Optional[str], end_date: Optional[str]) -> Optional[str]:
    if not start_date and not end_date:
        return None
    s = f"{start_date}T00:00:00Z" if start_date else ".."
    e = f"{end_date}T23:59:59Z" if end_date else ".."
    return f"{s}/{e}"


def build_search_body(bbox: Sequence[float], start_date: Optional[str], end_date: Optional[str],
                      collection: str, limit: int) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "collections": [check_collection(collection)],
        "bbox": [float(v) for v in bbox],
        "limit": int(limit),
        "sortby": [{"field": "datetime", "direction": "desc"}],
    }
    interval = _datetime_interval(start_date, end_date)
    if interval:
        body["datetime"] = interval
    return body


def search(bbox: Sequence[float], start_date: Optional[str] = None, end_date: Optional[str] = None,
           collection: str = DEFAULT_COLLECTION, limit: int = 10) -> List[Dict[str, Any]]:
    """Compact items intersecting ``bbox`` (lon/lat), newest first."""
    body = build_search_body(bbox, start_date, end_date, collection, limit)
    data = _request_json(f"{STAC_ROOT}/search", body)
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        raise StacError("unexpected STAC search response")
    items = [compact_item(f, bbox) for f in feats]
    items.sort(key=lambda i: i.get("datetime") or "", reverse=True)
    return items[: int(limit)]


def get_item(collection: str, item_id: str) -> Dict[str, Any]:
    """Raw STAC item (asset hrefs unsigned). Internal use; do not return to clients."""
    check_collection(collection)
    if not isinstance(item_id, str) or not _ITEM_RE.match(item_id):
        raise ValueError(f"invalid item id {item_id!r}")
    item = _request_json(f"{STAC_ROOT}/collections/{collection}/items/{urllib.parse.quote(item_id)}")
    if not isinstance(item, dict) or "assets" not in item:
        raise StacError("unexpected STAC item response")
    return item


_token_lock = threading.Lock()
_tokens: Dict[str, Tuple[str, float]] = {}


def _parse_expiry(s: Any) -> float:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except ValueError:
        return _now() + 600.0


def get_token(collection: str) -> str:
    """SAS token for ``collection`` (thread-safe cache). Never log or return it."""
    check_collection(collection)
    with _token_lock:
        cached = _tokens.get(collection)
        if cached and cached[1] - TOKEN_REFRESH_MARGIN_S > _now():
            return cached[0]
        data = _request_json(f"{SAS_ROOT}/{collection}")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise StacError("token endpoint returned no token")
        entry = (token.lstrip("?"), _parse_expiry(data.get("msft:expiry")))
        _tokens[collection] = entry
        return entry[0]


def clear_token_cache() -> None:
    with _token_lock:
        _tokens.clear()


def sign_href(href: str, collection: str = DEFAULT_COLLECTION) -> str:
    """Return ``href`` with a SAS token appended. The result is a secret.

    The token is only ever sent to Azure Blob Storage (where Planetary Computer assets live),
    never to an arbitrary host named in a STAC item.
    """
    host = (urllib.parse.urlsplit(href).hostname or "").lower()
    if urllib.parse.urlsplit(href).scheme != "https" or not host.endswith(".blob.core.windows.net"):
        raise StacError(f"refusing to sign a non-Azure-Blob asset URL ({redact(href)[:120]})")
    return href.split("?", 1)[0] + "?" + get_token(collection)
