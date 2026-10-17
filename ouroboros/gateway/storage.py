"""Dashboard Storage routes over one ``ouroboros.storage.StorageObserver`` per server."""

from __future__ import annotations

import pathlib
from typing import List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ouroboros.storage import StorageObserver

_NO_STORE = {"Cache-Control": "no-store"}


def storage_routes(data_dir: pathlib.Path, *, observer: Optional[StorageObserver] = None) -> List[Route]:
    owner = observer or StorageObserver(data_dir)

    async def api_storage(_request: Request) -> JSONResponse:
        """Passive: the last measurement held by this process; never walks, never writes."""
        return JSONResponse(owner.projection(), headers=_NO_STORE)

    async def api_storage_measure(_request: Request) -> JSONResponse:
        """Start one bounded walk, or join the running one; the request body is ignored."""
        started = owner.start()
        return JSONResponse({**owner.projection(), "started": started}, status_code=202, headers=_NO_STORE)

    return [
        Route("/api/storage", endpoint=api_storage, methods=["GET"]),
        Route("/api/storage/measure", endpoint=api_storage_measure, methods=["POST"]),
    ]
