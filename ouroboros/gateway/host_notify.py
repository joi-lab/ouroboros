"""``POST /notify``: a reviewed skill tells the owner one thing, now.

A skill holding the owner's ``notify_owner`` grant hands the host one short plain
sentence; the host puts it in the owner's chat as one System row
(``system_type="skill_notice"``) that opens with the signature line
``Notice · <skill>``, through the same ``send_with_budget`` seam every host row
uses. Like any System row it reaches every UI surface, the Telegram mirror and
the next turn's context (``📋 [skill_notice]``); no model turn starts. A disabled
skill, a stale review or a revoked grant is refused before anything is written
(``_authenticated``). Timing stays with the skill: a calendar keeps its own clock
and posts at the moment it chose. Beside ``host_service.py`` on the same loopback
trust boundary; ``create_host_service_app`` mounts it.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import run_sync_to_completion
from ouroboros.gateway.host_service import HostServiceAuthError, HostServiceContext, _authenticated, _json_error
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

NOTIFY_TEXT_MAX_CHARS = 400


class _ChatUnavailable(RuntimeError):
    """No chat writer serves this data root (no running supervisor): nothing was written."""


def _show_notice(data_dir: pathlib.Path, skill_name: str, text: str) -> Dict[str, Any]:
    from supervisor import message_bus
    from supervisor.schedule_notes import owner_chat_id

    root = message_bus.DATA_DIR
    if root is None or pathlib.Path(root).resolve() != pathlib.Path(data_dir).resolve():
        raise _ChatUnavailable("no chat writer serves this data root")
    if message_bus.try_get_bridge() is None:
        raise _ChatUnavailable("the chat bridge is not running")
    chat_id, ts = owner_chat_id(data_dir), utc_now_iso()
    message_bus.send_with_budget(
        chat_id, f"Notice · {skill_name}\n{text}", ts=ts, role="system", system_type="skill_notice",
        require_write=True, ensure_record_boundary=True, progress_meta={"source": skill_name},
    )
    return {"ts": ts, "chat_id": chat_id}


def _not_confirmed(error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "status": "not_confirmed", "error": error}, status_code=503)


async def _api_notify(request: Request) -> JSONResponse:
    """``{"text": ...}`` → ``200 {ok, ts, chat_id}`` once the owner's chat row is written.

    ``400`` a body that is not one plain sentence, ``403`` no live grant, ``429``
    the per-skill lane, ``503`` the row is not confirmed: check the chat before
    sending it again (an immediate notice is not server-idempotent)."""
    ctx: HostServiceContext = request.app.state.host_service_context
    try:
        skill_name, _ = await _authenticated(ctx, request.headers.get("x-skill-token", ""), "notify_owner")
    except HostServiceAuthError as exc:
        return _json_error(str(exc), 403)
    if not ctx.rate_limiter.allow(f"{skill_name}:notify"):
        return _json_error("rate limit exceeded", 429)
    try:
        payload = await request.json()
    except Exception:
        return _json_error("invalid json", 400)
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        return _json_error("text is required: one plain sentence for the owner", 400)
    text = text.strip()
    if len(text) > NOTIFY_TEXT_MAX_CHARS:
        return _json_error(f"text must be at most {NOTIFY_TEXT_MAX_CHARS} characters", 400)
    try:
        shown = await run_sync_to_completion(_show_notice, ctx.data_dir, skill_name, text)
    except _ChatUnavailable as exc:
        return _not_confirmed(f"the owner's chat is not available ({exc}); nothing was written")
    except Exception:
        log.warning("Notice from skill %s was not confirmed in the owner's chat", skill_name, exc_info=True)
        return _not_confirmed("write not confirmed; check the owner's chat before sending it again")
    return JSONResponse({"ok": True, **shown})
