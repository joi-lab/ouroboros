"""Core update flow envelopes: ``/api/update/preflight``, ``/api/update/apply`` (and the rollback
refusal that shares its error shape) and the two update WS frames; re-exported by ``gateway/contracts.py``.
The JSDoc twin stays in ``web/modules/api_types.js``."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    from typing import Literal, NotRequired, TypedDict  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - CI supports Python 3.10.
    from typing_extensions import Literal, NotRequired, TypedDict  # type: ignore[assignment]


class UpdateMergePlan(TypedDict, total=False):
    """Exact, channel-bound merge plan shared by preflight and apply responses."""

    available: bool
    auto_mergeable: bool
    kind: Literal["clean", "conflicting", "current", "unavailable", "unknown"]
    error: str
    remote: str
    remote_branch: str
    target_ref: str
    update_channel: str
    current_branch: str
    base_sha: str
    target_sha: str
    local_dirty_count: int
    local_snapshot: str
    merge_commit: str
    code_conflict_paths: List[str]
    doc_conflict_paths: List[str]
    hot_code_paths: List[str]
    recommended_strategy: Literal["auto_merge", "assisted"]


class UpdatePreflightRequest(TypedDict, total=False):
    """POST /api/update/preflight has an intentionally empty JSON body."""


class UpdatePreflightResponse(TypedDict):
    merge_plan: UpdateMergePlan


class UpdateApplyRequest(TypedDict):
    strategy: Literal["auto_merge", "assisted", "manual", "replace"]
    expected_base_sha: NotRequired[str]
    expected_target_sha: NotRequired[str]
    confirm_recovery: NotRequired[bool]


class UpdateApplySuccessResponse(TypedDict):
    status: Literal["ok", "restart_required", "assisted_started", "manual"]
    restarting: NotRequired[bool]
    strategy: NotRequired[Literal["auto_merge", "assisted", "manual", "replace"]]
    task_id: NotRequired[str]
    merge_plan: NotRequired[UpdateMergePlan]
    error: NotRequired[str]


class UpdateApplyErrorResponse(TypedDict):
    error: str
    reason: NotRequired[str]
    abandoned: NotRequired[bool]
    status: NotRequired[Dict[str, Any]]  # preparation observations, not the success discriminator
    update_intent: NotRequired[Dict[str, Any]]
    rescue: NotRequired[Dict[str, Any]]
    keep_branch: NotRequired[str]
    blockers: NotRequired[List[str]]
    rolled_back: NotRequired[bool]
    rollback: NotRequired[str]
    restart_required: NotRequired[bool]
    merge_plan: NotRequired[UpdateMergePlan]
    smoke: NotRequired[Dict[str, Any]]
    # Stash-first prologue disclosures (additive): how the owner's stashed work
    # was unwound on an aborted update. The wave numbers rode the retired
    # wave-floor refusal (#1487 removed it); the optional keys stay in the shape.
    stash_note: NotRequired[str]
    estimated_wave_usd: NotRequired[Optional[float]]
    remaining_usd: NotRequired[Optional[float]]


class UpdateProgressChangedOutbound(TypedDict):
    """Invalidation only; never proof that boot finalization completed."""
    type: Literal["update_progress_changed"]


class UpdateStatusReadyOutbound(TypedDict):
    type: Literal["update_status_ready"]
    available: bool
    check_ok: Optional[bool]


__all__ = [
    "UpdateMergePlan", "UpdatePreflightRequest", "UpdatePreflightResponse", "UpdateApplyRequest",
    "UpdateApplySuccessResponse", "UpdateApplyErrorResponse", "UpdateProgressChangedOutbound",
    "UpdateStatusReadyOutbound",
]
