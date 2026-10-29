"""Per-mount, temporary read capabilities for a publication's declared browser files.

Only NetworkAuthGate admits the read prefix without normal network authentication.
Controls stay on /api/widgets/asset-leases, outside every child fetch-bridge prefix.
The gate owns one ephemeral lease table; restart drops it. No lease authenticates
an API, executes a plugin handler, or discovers files at read time.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import secrets
import threading
import time
import weakref
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ouroboros.extension_liveness import widget_publication_ready
from ouroboros.extension_registry_state import live_widget_files
from ouroboros.extension_widget_assets import WIDGET_ASSET_TYPES, widget_asset_path
from ouroboros.gateway._helpers import request_drive_root

ASSET_PREFIX = "/api/widget-assets/"
LEASE_TTL_SECONDS = 120
LEASE_RENEW_SECONDS = 40
MAX_LEASES = 512
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Access-Control-Allow-Origin": "null", "Vary": "Origin", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "sandbox; default-src 'none'"}


def redact_asset_links(text: str) -> str:
    return re.sub(r"(/api/widget-assets/)[A-Za-z0-9_-]+", r"\1[redacted]", text)


@dataclass(frozen=True)
class _Lease:
    owner: str
    skill: str
    tab_id: str
    generation: str
    drive_root: pathlib.Path
    expires: float


@dataclass
class _MintAdmission:
    cancelled: bool = False


class WidgetAssetLeases:
    """Ephemeral mount lifecycle; immutable bytes remain owned by the live bundle."""

    def __init__(self):
        self._lock = threading.RLock()
        self._leases: dict[str, _Lease] = {}
        # Only in-flight controls retain admissions. Logout also cancels mints
        # queued for a worker; this is not a persistent session revocation list.
        self._admissions: weakref.WeakValueDictionary[str, _MintAdmission] = weakref.WeakValueDictionary()
        self._password = None

    def admit(self, owner: str) -> _MintAdmission:
        with self._lock:
            admission = self._admissions.get(owner)
            if admission is None:
                admission = _MintAdmission()
                self._admissions[owner] = admission
            return admission

    def password_changed(self, password: str) -> None:
        with self._lock:
            if self._password != password:
                for admission in self._admissions.values():
                    admission.cancelled = True
                self._admissions.clear()
                self._leases.clear()
                self._password = password

    def revoke_owner(self, owner: str) -> None:
        with self._lock:
            admission = self._admissions.pop(owner, None)
            if admission is not None:
                admission.cancelled = True
            self._leases = {key: lease for key, lease in self._leases.items() if lease.owner != owner}

    def _prune(self) -> None:
        now = time.monotonic()
        self._leases = {key: lease for key, lease in self._leases.items() if lease.expires > now}

    def _files(self, token: str, lease: _Lease):
        if widget_publication_ready(lease.skill, lease.generation, lease.drive_root):
            files = live_widget_files(lease.skill, lease.tab_id, lease.generation)
            if files is not None:
                return files
        self._leases.pop(token, None)
        return None

    def open(self, owner: str, skill: str, tab_id: str, generation: str, drive_root: pathlib.Path,
             epoch: str, admission: _MintAdmission):
        with self._lock:
            if epoch != self._password or admission.cancelled:
                return None
            self._prune()
            lease = _Lease(owner, skill, tab_id, generation, drive_root, time.monotonic() + LEASE_TTL_SECONDS)
            if self._files("", lease) is None:
                return None
            if len(self._leases) >= MAX_LEASES:
                return None
            token = secrets.token_urlsafe(32)
            self._leases[token] = lease
            return self._description(token)

    @staticmethod
    def _description(token: str) -> dict:
        return {"token": token, "base_url": f"{ASSET_PREFIX}{token}/",
                "expires_in": LEASE_TTL_SECONDS, "renew_after": LEASE_RENEW_SECONDS}

    def renew(self, owner: str, token: str, epoch: str):
        with self._lock:
            if epoch != self._password:
                return None
            self._prune()
            lease = self._leases.get(token)
            if lease is None or lease.owner != owner or self._files(token, lease) is None:
                return None
            self._leases[token] = _Lease(lease.owner, lease.skill, lease.tab_id, lease.generation,
                                         lease.drive_root, time.monotonic() + LEASE_TTL_SECONDS)
            return self._description(token)

    def revoke(self, owner: str, token: str) -> None:
        with self._lock:
            lease = self._leases.get(token)
            if lease is not None and lease.owner == owner:
                del self._leases[token]

    def read(self, token: str, path: str):
        with self._lock:
            self._prune()
            lease = self._leases.get(token)
            if lease is None:
                return None
            files = self._files(token, lease)
            return files.get(path) if files is not None else None


async def widget_asset_response(request: Request, leases: WidgetAssetLeases) -> Response:
    """Reserved prefix: no fallthrough, cookie fallback, redirects or write methods."""
    token, _, path = request.scope["path"][len(ASSET_PREFIX):].partition("/")
    # Uvicorn's access writer holds this scope. Never log the bearer path/query,
    # including errors, and do not put it into a login redirect's next parameter.
    request.scope["path"] = ASSET_PREFIX + "[redacted]/" + path
    request.scope["raw_path"] = request.scope["path"].encode("utf-8")
    request.scope["query_string"] = b""
    status, message = 404, "widget asset unavailable"
    if request.method not in {"GET", "HEAD"}:
        status, message = 405, "widget assets are read-only"
    elif not _TOKEN.fullmatch(token) or not widget_asset_path(path):
        status, message = 400, "invalid widget asset path"
    else:
        content = await asyncio.to_thread(leases.read, token, path)
        if content is not None:
            headers = {**_HEADERS, "Content-Length": str(len(content))}
            return Response(b"" if request.method == "HEAD" else content,
                            media_type=WIDGET_ASSET_TYPES[pathlib.PurePosixPath(path).suffix], headers=headers)
    return JSONResponse({"error": message}, status_code=status, headers=_HEADERS)


def _from_parent_page(request: Request) -> bool:
    """Same-origin parent page, never the opaque frame or another site. The browser's own Sec-Fetch-Site, which no
    page script can set, decides even behind a proxy that rewrites Host or terminates TLS; without Fetch Metadata,
    Origin must name the URL this server sees."""
    origin = request.headers.get("origin")
    site = request.headers.get("sec-fetch-site")
    if origin == "null":
        return False
    if site is not None:
        return site in {"same-origin", "none"}
    return origin is None or origin == f"{request.url.scheme}://{request.url.netloc}"


async def api_widget_asset_lease(request: Request) -> Response:
    """Parent-authenticated JSON controls; opaque and cross-site browser requests are refused."""
    if not _from_parent_page(request):
        return JSONResponse({"error": "widget lease controls require the parent origin"}, status_code=403)
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        return JSONResponse({"error": "expected JSON"}, status_code=400)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 2048:
            return JSONResponse({"error": "lease request too large"}, status_code=413)
    import json

    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected lease object"}, status_code=400)
    leases = request.scope["widget_asset_leases"]
    owner = request.scope["widget_asset_owner"]
    epoch = request.scope["widget_asset_epoch"]
    op = request.path_params.get("op")
    fields = ("skill", "tab_id", "generation") if op is None else ("token",)
    if set(body) != set(fields) or not all(isinstance(body[k], str) and 0 < len(body[k]) <= 128 for k in fields):
        return JSONResponse({"error": "invalid lease fields"}, status_code=400)
    if op is None:
        result = await asyncio.to_thread(leases.open, owner, body["skill"], body["tab_id"],
                                         body["generation"], request_drive_root(request), epoch,
                                         request.scope["widget_asset_admission"])
    elif op == "renew":
        result = await asyncio.to_thread(leases.renew, owner, body["token"], epoch)
    elif op == "revoke":
        leases.revoke(owner, body["token"])
        result = {"ok": True}
    else:
        return JSONResponse({"error": "unknown lease operation"}, status_code=404)
    return JSONResponse(result or {"error": "widget publication or lease unavailable"},
                        status_code=200 if result else 409, headers={"Cache-Control": "no-store"})
