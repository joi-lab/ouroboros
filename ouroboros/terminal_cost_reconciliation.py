"""Terminal maintenance's cost duty: recovery first, then projection selection.

The equality memo is ONLY proof that the same observed projection needs no
write. It never authorizes recovery, declares ownership dead, or settles money.
No bodies, negative results, write outcomes, timers or durable state are cached.
"""
from __future__ import annotations

import logging
import pathlib
import stat
from copy import deepcopy
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EqualProjection:
    stamp: tuple
    scopes: tuple[bool, str]
    authority_path: str
    buckets: tuple


# The existing maintenance latch serializes production passes. Entries are
# disposable; restart or eviction only repeats an ordinary comparison.
_EQUAL_PROJECTIONS: dict[tuple[str, str], _EqualProjection] = {}


def _result_stamp(path: pathlib.Path) -> tuple:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Cost projection result is not a regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _buckets(breakdown: dict, task_id: str, scopes: tuple[bool, str]) -> tuple:
    """Full relevant buckets, including absence, never the global ledger sequence.

    Only inserts deepcopy this basis: a later append can mutate a caller's view
    without retrospectively changing the equality we actually observed.
    """
    own = breakdown["by_task"]
    roots = breakdown["by_root"]
    is_root, root_id = scopes
    return (bool(breakdown.get("integrity_degraded")),
            (task_id in own, own.get(task_id)),
            (root_id in roots, roots.get(root_id)) if is_root else None)


def _refresh_costs(root: pathlib.Path, owners: set[str], recovery_tasks: dict) -> None:
    from ouroboros import usage_accounting as usage
    from ouroboros.task_results import load_task_result, task_result_path
    from supervisor.events_task_done import _refresh_terminal_task_cost, _terminal_cost_probe
    from supervisor.queue import task_has_live_ownership

    root_key = str(root)
    for key in list(_EQUAL_PROJECTIONS):
        if key[0] == root_key and key[1] not in owners:
            del _EQUAL_PROJECTIONS[key]
    if not owners:
        return
    try:
        usage.ensure_legacy_imported(root)
        # One post-recovery view; a failure never turns into per-owner fallback.
        breakdown = usage.usage_breakdown(root)
    except Exception:
        for key in list(_EQUAL_PROJECTIONS):
            if key[0] == root_key:
                del _EQUAL_PROJECTIONS[key]
        log.warning("Reconciled usage projection unavailable", exc_info=True)
        return
    for task_id in sorted(owners):
        key = root_key, task_id
        try:
            # Reuse only a conservative recovery exclusion, NEVER its permission
            # to mutate: a direct/review/retry owner can appear later in the pass.
            if task_id in recovery_tasks and recovery_tasks[task_id] is None:
                _EQUAL_PROJECTIONS.pop(key, None)
                continue
            path = task_result_path(root, task_id, create=False)
            before = _result_stamp(path)
            cached = _EQUAL_PROJECTIONS.get(key)
            if (cached is not None and cached.stamp == before
                    and pathlib.Path(cached.authority_path).resolve() == root
                    and cached.buckets == _buckets(breakdown, task_id, cached.scopes)):
                continue
            _EQUAL_PROJECTIONS.pop(key, None)
            current = load_task_result(root, task_id, strict=True) or {}
            if _result_stamp(path) != before:
                continue  # a concurrent writer supplies next pass's inputs
            outcome, fields, scopes = _terminal_cost_probe(
                root, task_id, current, breakdown=breakdown, canonical_only=True)
            if outcome == "equal":
                authority_path = str(current.get("budget_drive_root") or root)
                # Equality proves only that these exact inputs need no rewrite;
                # unknown/non-final cost stays so, and recovery runs independently.
                if (fields.get("accounted_upper_bound_usd") is not None
                        and not breakdown.get("integrity_degraded")
                        and (not scopes[0] or fields.get("accounted_upper_bound_usd_with_children") is not None)
                        and pathlib.Path(authority_path).resolve() == root
                        and _result_stamp(path) == before):
                    _EQUAL_PROJECTIONS[key] = _EqualProjection(
                        before, scopes, authority_path, deepcopy(_buckets(breakdown, task_id, scopes)))
                continue
            if outcome not in {"differs", "foreign"}:
                continue
            # Foreign authority goes through this guard BEFORE accounting/import.
            # The writer rereads/recomputes; never carry early fields into a write.
            if not task_has_live_ownership(task_id):
                _refresh_terminal_task_cost(root, task_id, breakdown=breakdown)
            # Even a successful write needs a subsequent stable EQUAL read.
        except FileNotFoundError:
            _EQUAL_PROJECTIONS.pop(key, None)  # absence is not a lasting exclusion
        except Exception:
            _EQUAL_PROJECTIONS.pop(key, None)
            log.warning("Reconciled task cost refresh failed for %s", task_id, exc_info=True)


def reconcile_abandoned_usage(drive_root: pathlib.Path) -> None:
    """Close unowned terminal-task attempts; price and remote custody stay separate."""
    from ouroboros import usage_accounting as usage
    from ouroboros.claudexor_daemon import read_owned_gateway
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    from ouroboros.llm_claudexor import recover_model_attempt
    from ouroboros.post_task_checkpoint import post_task_synthesis_is_open
    from ouroboros.task_results import load_task_result, validate_task_id
    from ouroboros.task_status import SETTLED_STATUSES
    from ouroboros.transport_custody import ProviderNotDispatched, release_pre_dispatch_attempt
    from ouroboros.usage_ledger import is_abandoned_settlement
    from supervisor.queue import task_has_live_ownership

    root = pathlib.Path(drive_root).resolve()
    rows = usage.read_usage_records(root, final_only=True)
    tasks, refresh = {}, set()
    gateway, gateway_unavailable = None, False

    def eligible_task(task_id):
        if not task_id:
            return False
        if task_id not in tasks:
            try:
                task = load_task_result(root, task_id, strict=True) or {}
                checkpoint = task.get("root_phase_checkpoint") or {}
                tasks[task_id] = task if (
                    task.get("status") in SETTLED_STATUSES
                    and not task_has_live_ownership(task_id)
                    and not post_task_synthesis_is_open(checkpoint.get("post_task_synthesis"))
                ) else None
            except Exception:
                tasks[task_id] = None  # Unreadable ownership permits neither duty.
        return tasks[task_id] is not None

    def borrowed_gateway():
        nonlocal gateway, gateway_unavailable
        if gateway_unavailable:
            raise ClaudexorUnavailable("daemon_unreachable", "Usage recovery deferred until the next maintenance pass")
        if gateway is None:
            try:
                gateway = read_owned_gateway()
            except Exception:
                gateway_unavailable = True
                raise
        return gateway

    try:
        for row in rows:
            kind = row.get("kind", "attempt")
            if kind not in {"attempt", "usage_baseline_group"} or any(row.get(key) for key in usage.REVIEW_ATTRIBUTION_KEYS):
                continue
            task_id = str(row.get("task_id") or "")
            # Settled/compacted attribution still owes projection after a failed write.
            for owner in (task_id, str(row.get("root_task_id") or "")):
                try:
                    validate_task_id(owner)
                except ValueError:
                    continue  # Empty or system:* accounting scopes own no task result.
                refresh.add(owner)
            remote = row.get("provider") == "claudexor"
            abandoned = is_abandoned_settlement(row)
            if (kind != "attempt"
                or (row.get("state") not in {"reserved", "dispatched", "unresolved"}
                    and not (remote and abandoned))):
                continue
            if not eligible_task(task_id):
                continue
            reservation = usage.AttemptReservation(
                str(row["attempt_id"]), root, str(row.get("model") or ""),
                str(row.get("provider") or ""), row.get("reservation_upper_bound_usd"),
                str(row.get("processing_preference") or ""), str(row.get("submitted_processing_mode") or ""),
                row.get("processing_basis"),
            )
            try:
                recovered = None
                if remote and row.get("state") != "reserved":
                    recovered = recover_model_attempt(root, row, gateway_factory=borrowed_gateway)
                    if recovered is None:
                        continue
                disposition, reported, cost, final = recovered or ("abandoned", {}, None, False)
                if disposition == "settled":
                    usage.settle_attempt(reservation, reported, cost_usd=cost, cost_final=final)
                elif disposition == "released":
                    if not release_pre_dispatch_attempt(reservation, ProviderNotDispatched("recovered model operation never started")):
                        continue
                elif disposition == "abandoned":
                    if abandoned:
                        continue
                    state = usage.terminalize_abandoned_attempt(reservation, reason="owner_task_terminal", expected_seq=row.get("seq"))
                    if state not in {"settled", "released"}:
                        continue
                else:
                    continue
            except ClaudexorUnavailable as exc:
                gateway_unavailable = gateway_unavailable or exc.code == "daemon_unreachable"
                log.debug("Model usage custody deferred for %s: %s", row["attempt_id"], exc.code)
            except Exception:
                log.warning("Usage reconciliation deferred for %s", row["attempt_id"], exc_info=True)
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                log.debug("Usage recovery gateway close failed", exc_info=True)
    _refresh_costs(root, refresh, tasks)
