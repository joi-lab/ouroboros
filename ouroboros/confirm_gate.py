"""
Confirm-gate for high-risk tool calls.

Flow:
1) Sensitive tool call arrives.
2) If matching approved request exists -> consume approval and allow call.
3) Else create/find pending request and block call with CONFIRM_REQUIRED message.
4) Owner approves via Telegram command: /approve <request_id>.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import sanitize_tool_args_for_log


DRIVE_ROOT: pathlib.Path = pathlib.Path("/content/drive/MyDrive/Ouroboros")
CONFIRMATIONS_PATH: pathlib.Path = DRIVE_ROOT / "state" / "confirmations.json"
CONFIRMATIONS_LOCK_PATH: pathlib.Path = DRIVE_ROOT / "locks" / "confirmations.lock"

REQUEST_TTL_SEC = 12 * 60 * 60
MAX_RECORDS = 300

_SENSITIVE_SHELL_PATTERNS = (
    " token",
    "token=",
    "api_key",
    "apikey",
    "secret",
    "password",
    "passphrase",
    "gh auth",
    "gcloud auth",
    "aws configure",
    "git remote set-url",
    "telegram_bot_token",
    "openrouter_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "github_token",
)

_POLICY_FILE_MARKERS = (
    "bible.md",
    "prompts/system.md",
    "prompts/consciousness.md",
    "viktor_north_star.md",
    "docs/meta_governance.md",
)


def init(drive_root: pathlib.Path) -> None:
    global DRIVE_ROOT, CONFIRMATIONS_PATH, CONFIRMATIONS_LOCK_PATH
    DRIVE_ROOT = pathlib.Path(drive_root).resolve()
    CONFIRMATIONS_PATH = DRIVE_ROOT / "state" / "confirmations.json"
    CONFIRMATIONS_LOCK_PATH = DRIVE_ROOT / "locks" / "confirmations.lock"


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _ts_now() -> float:
    return time.time()


def _to_ts(iso_val: str) -> float:
    if not iso_val:
        return 0.0
    try:
        return dt.datetime.fromisoformat(iso_val.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _acquire_lock(timeout_sec: float = 4.0, stale_sec: float = 120.0) -> Optional[int]:
    CONFIRMATIONS_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    start = _ts_now()
    while (_ts_now() - start) < timeout_sec:
        try:
            fd = os.open(str(CONFIRMATIONS_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, f"pid={os.getpid()} ts={_utc_now_iso()}\n".encode("utf-8"))
            except Exception:
                pass
            return fd
        except FileExistsError:
            try:
                age = _ts_now() - CONFIRMATIONS_LOCK_PATH.stat().st_mtime
                if age > stale_sec:
                    CONFIRMATIONS_LOCK_PATH.unlink()
                    continue
            except Exception:
                pass
            time.sleep(0.05)
        except Exception:
            break
    return None


def _release_lock(fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            pass
    try:
        if CONFIRMATIONS_LOCK_PATH.exists():
            CONFIRMATIONS_LOCK_PATH.unlink()
    except Exception:
        pass


def _load_state_unlocked() -> Dict[str, Any]:
    if not CONFIRMATIONS_PATH.exists():
        return {"requests": []}
    try:
        data = json.loads(CONFIRMATIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"requests": []}
    if not isinstance(data, dict):
        return {"requests": []}
    reqs = data.get("requests")
    if not isinstance(reqs, list):
        data["requests"] = []
    return data


def _save_state_unlocked(state: Dict[str, Any]) -> None:
    CONFIRMATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    tmp = CONFIRMATIONS_PATH.with_name(f".{CONFIRMATIONS_PATH.name}.tmp.{uuid.uuid4().hex}")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(str(tmp), str(CONFIRMATIONS_PATH))


def _expire_and_trim_locked(state: Dict[str, Any]) -> bool:
    changed = False
    now = _ts_now()
    reqs = state.get("requests") or []
    if not isinstance(reqs, list):
        reqs = []
        state["requests"] = reqs
        changed = True
    for req in reqs:
        if not isinstance(req, dict):
            continue
        status = str(req.get("status") or "")
        if status not in {"pending", "approved"}:
            continue
        expires_at = str(req.get("expires_at") or "")
        if expires_at and now > _to_ts(expires_at):
            req["status"] = "expired"
            req["expired_at"] = _utc_now_iso()
            changed = True
    if len(reqs) > MAX_RECORDS:
        state["requests"] = reqs[-MAX_RECORDS:]
        changed = True
    return changed


def _is_sensitive_shell(cmd: Any) -> bool:
    if isinstance(cmd, list):
        joined = " ".join(str(x) for x in cmd).lower()
    else:
        joined = str(cmd or "").lower()
    if not joined.strip():
        return False
    return any(p in joined for p in _SENSITIVE_SHELL_PATTERNS)


def _classify_call(tool_name: str, args: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    name = str(tool_name or "").strip()
    if not name:
        return None, None, None

    if name in {"repo_commit_push", "create_github_issue", "comment_on_issue", "close_github_issue"}:
        return "external_publication", "External publication action", name

    if name == "repo_write_commit":
        raw_path = str(args.get("path") or "").replace("\\", "/").lower()
        if any(marker in raw_path for marker in _POLICY_FILE_MARKERS):
            return "mission_policy_change", f"Policy file update: {raw_path}", name
        return "external_publication", "Code write+push action", name

    if name == "toggle_evolution" and bool(args.get("enabled")):
        return "budget_risk", "Enable evolution mode (may consume budget fast)", name

    if name == "run_shell" and _is_sensitive_shell(args.get("cmd")):
        return "credentials_or_access", "Sensitive shell command (credentials/access)", name

    return None, None, None


def _action_key(tool_name: str, category: str, args: Dict[str, Any]) -> str:
    args_for_key = sanitize_tool_args_for_log(tool_name, args or {}, threshold=800)
    payload = json.dumps(
        {"tool": tool_name, "category": category, "args": args_for_key},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _find_request(
    requests: List[Dict[str, Any]],
    *,
    action_key: str = "",
    request_id: str = "",
    statuses: Tuple[str, ...] = ("pending",),
) -> Optional[Dict[str, Any]]:
    request_id_l = request_id.strip().lower()
    for req in reversed(requests):
        if not isinstance(req, dict):
            continue
        if statuses and str(req.get("status") or "") not in statuses:
            continue
        if action_key and str(req.get("action_key") or "") != action_key:
            continue
        if request_id and str(req.get("request_id") or "").lower() != request_id_l:
            continue
        return req
    return None


def _create_pending_request(
    state: Dict[str, Any],
    *,
    tool_name: str,
    category: str,
    reason: str,
    action_key: str,
    task_id: str = "",
) -> Dict[str, Any]:
    req_id = f"cfm-{uuid.uuid4().hex[:8]}"
    now_iso = _utc_now_iso()
    expires_iso = dt.datetime.fromtimestamp(_ts_now() + REQUEST_TTL_SEC, tz=dt.timezone.utc).isoformat()
    req = {
        "request_id": req_id,
        "tool": tool_name,
        "category": category,
        "reason": reason,
        "action_key": action_key,
        "status": "pending",
        "task_id": task_id or "",
        "created_at": now_iso,
        "expires_at": expires_iso,
    }
    reqs = state.setdefault("requests", [])
    if not isinstance(reqs, list):
        reqs = []
        state["requests"] = reqs
    reqs.append(req)
    return req


def guard_tool_call(
    drive_root: pathlib.Path,
    tool_name: str,
    args: Dict[str, Any],
    task_id: str = "",
) -> Optional[str]:
    """
    Returns None if tool call is allowed.
    Returns CONFIRM_REQUIRED message when explicit owner approval is needed.
    """
    init(pathlib.Path(drive_root))
    category, reason, normalized_tool = _classify_call(tool_name, args)
    if not category:
        return None

    action_key = _action_key(normalized_tool or tool_name, category, args)

    fd = _acquire_lock()
    if fd is None:
        return (
            "⚠️ CONFIRM_GATE_LOCK_ERROR: cannot verify confirmation lock now. "
            "Please retry this action."
        )
    created = False
    req: Optional[Dict[str, Any]] = None
    try:
        state = _load_state_unlocked()
        changed = _expire_and_trim_locked(state)
        requests = state.get("requests") or []
        if not isinstance(requests, list):
            requests = []
            state["requests"] = requests
            changed = True

        approved = _find_request(
            requests, action_key=action_key, statuses=("approved",),
        )
        if approved is not None:
            approved["status"] = "consumed"
            approved["consumed_at"] = _utc_now_iso()
            approved["consumed_by_tool"] = normalized_tool or tool_name
            _save_state_unlocked(state)
            return None

        req = _find_request(
            requests, action_key=action_key, statuses=("pending",),
        )
        if req is None:
            req = _create_pending_request(
                state,
                tool_name=normalized_tool or tool_name,
                category=category,
                reason=reason or "Sensitive action",
                action_key=action_key,
                task_id=task_id,
            )
            created = True
            changed = True

        if changed:
            _save_state_unlocked(state)
    finally:
        _release_lock(fd)

    assert req is not None
    prefix = "created" if created else "waiting"
    return (
        f"⚠️ CONFIRM_REQUIRED ({prefix}): {req.get('reason')}. "
        f"request_id={req.get('request_id')}. "
        f"Ask owner to send `/approve {req.get('request_id')}` in Telegram, then retry."
    )


def approve_request(
    drive_root: pathlib.Path,
    request_id: str,
    approver_id: int = 0,
    raw_text: str = "",
) -> Tuple[bool, str]:
    init(pathlib.Path(drive_root))
    rid = str(request_id or "").strip()
    if not rid:
        return False, "Missing request id. Usage: /approve cfm-XXXXXXXX"

    fd = _acquire_lock()
    if fd is None:
        return False, "Cannot acquire confirmation lock right now. Try again."
    try:
        state = _load_state_unlocked()
        changed = _expire_and_trim_locked(state)
        requests = state.get("requests") or []
        if not isinstance(requests, list):
            requests = []
            state["requests"] = requests
            changed = True

        req = _find_request(requests, request_id=rid, statuses=("pending",))
        if req is None:
            existing = _find_request(
                requests,
                request_id=rid,
                statuses=("approved", "consumed", "expired"),
            )
            if changed:
                _save_state_unlocked(state)
            if existing is None:
                return False, f"Request `{rid}` not found."
            status = str(existing.get("status") or "unknown")
            if status == "expired":
                return False, f"Request `{rid}` expired. Re-run action to create a new request."
            if status == "consumed":
                return False, f"Request `{rid}` already used."
            return False, f"Request `{rid}` already approved."

        req["status"] = "approved"
        req["approved_at"] = _utc_now_iso()
        req["approved_by"] = int(approver_id or 0)
        req["approval_note"] = str(raw_text or "")[:300]
        _save_state_unlocked(state)
    finally:
        _release_lock(fd)

    return True, f"Approved `{rid}`. Retry the blocked action."


def pending_requests(drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    init(pathlib.Path(drive_root))
    fd = _acquire_lock()
    if fd is None:
        return []
    try:
        state = _load_state_unlocked()
        changed = _expire_and_trim_locked(state)
        requests = state.get("requests") or []
        if changed:
            _save_state_unlocked(state)
    finally:
        _release_lock(fd)

    out: List[Dict[str, Any]] = []
    for req in reversed(requests if isinstance(requests, list) else []):
        if not isinstance(req, dict):
            continue
        if str(req.get("status") or "") != "pending":
            continue
        out.append(req)
    return out


def pending_requests_text(drive_root: pathlib.Path, limit: int = 6) -> str:
    reqs = pending_requests(drive_root)
    if not reqs:
        return "No pending confirmations."
    lines = ["Pending confirmations:"]
    for req in reqs[: max(1, int(limit or 1))]:
        rid = str(req.get("request_id") or "-")
        reason = str(req.get("reason") or "Sensitive action")
        category = str(req.get("category") or "risk")
        created = str(req.get("created_at") or "-")
        lines.append(f"- {rid} | {category} | {reason} | created {created}")
    return "\n".join(lines)


def request_id_from_text(text: str) -> str:
    """Extract request id from arbitrary text. Expected format: cfm-xxxxxxxx."""
    m = re.search(r"\bcfm-[a-f0-9]{8}\b", str(text or "").lower())
    return m.group(0) if m else ""
