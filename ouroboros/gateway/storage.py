"""Dashboard Storage routes over the one ``ouroboros.storage.count`` owner of this server and the one
``ouroboros.storage_free.free`` owner of the Temporary space tray."""

from __future__ import annotations

import pathlib
from typing import Any, List, Optional

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ouroboros import storage, storage_free

_NO_STORE = {"Cache-Control": "no-store"}


def storage_routes(data_dir: pathlib.Path, *, repo_dir: Optional[Any] = None,
                   owner: Optional[storage.StorageCounter] = None,
                   free_owner: Optional[storage_free.FreeSpace] = None) -> List[Route]:
    counter = owner or storage.count
    tray = free_owner or storage_free.free

    def repo() -> pathlib.Path:
        if repo_dir is not None:
            return pathlib.Path(repo_dir)
        from ouroboros.config import REPO_DIR

        return pathlib.Path(REPO_DIR)

    async def api_storage(request: Request) -> JSONResponse:
        """Passive: the saved count, the running count's progress, fresh free space; never
        walks, never writes. ``?count=<finished_at>`` omits a count the client already holds."""
        token = request.query_params.get("count") or None
        return JSONResponse(await run_in_threadpool(counter.projection, data_dir, count_token=token, free=tray),
                            headers=_NO_STORE)

    async def answer(**extra: Any) -> JSONResponse:
        projection = await run_in_threadpool(counter.projection, data_dir, free=tray)
        return JSONResponse({**projection, **extra}, status_code=202, headers=_NO_STORE)

    async def api_storage_measure(_request: Request) -> JSONResponse:
        """Start one complete count in the child, or join the running one; the body is ignored."""
        return await answer(started=counter.start(data_dir, repo()))

    async def api_storage_measure_cancel(_request: Request) -> JSONResponse:
        """Kill the running count; the saved count stays. Nothing running answers the same way."""
        counter.cancel()
        return await answer()

    async def api_storage_free_preview(_request: Request) -> JSONResponse:
        """List, without side effects, what the app's own retirers would remove now; a second
        preview joins the running one and nothing starts while a run runs."""
        return await answer(started=tray.preview(data_dir, repo()))

    async def api_storage_free(request: Request) -> JSONResponse:
        """Free exactly the previewed items of the chosen groups: ``{"plan": "<id>", "groups": [...]}``;
        a missing or replaced plan answers ``started: false, reason: "preview_outdated"``."""
        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}
        outcome = tray.start(data_dir, repo(), body.get("plan"), body.get("groups"))
        return await answer(**outcome)

    async def api_storage_free_cancel(_request: Request) -> JSONResponse:
        """Stop the listing (nothing is kept) or the run (what was freed stays freed)."""
        tray.cancel()
        return await answer()

    return [
        Route("/api/storage", endpoint=api_storage, methods=["GET"]),
        Route("/api/storage/measure", endpoint=api_storage_measure, methods=["POST"]),
        Route("/api/storage/measure/cancel", endpoint=api_storage_measure_cancel, methods=["POST"]),
        Route("/api/storage/free/preview", endpoint=api_storage_free_preview, methods=["POST"]),
        Route("/api/storage/free", endpoint=api_storage_free, methods=["POST"]),
        Route("/api/storage/free/cancel", endpoint=api_storage_free_cancel, methods=["POST"]),
    ]
