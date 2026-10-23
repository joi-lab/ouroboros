"""Body-owned observation and mechanical collection of exact delegated runs.

The author still owns questions, consumption and disposition. This owner stores
execution facts, full source bytes, captures and supplied money independently of
that author's attention. Observation and heavy collection use separate physical
jobs; one slow capture never becomes the clock of another run's wake.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from ouroboros import delegate_custody as c
from ouroboros import delegate_custody_current as current
from ouroboros.delegate_custody_current import collection_pending
from ouroboros._usage_rows import REVIEW_ATTRIBUTION_KEYS

log = logging.getLogger(__name__)


def _shape(entry):
    from ouroboros.subagents import DelegatedRunShape
    from ouroboros.delegate_registration_policy import INVESTIGATION_AUTHORITY
    return DelegatedRunShape(access=entry.access, mode=entry.mode, isolation=entry.isolation,
                             delegated=entry.delegated,
                             investigation=entry.authority_source == INVESTIGATION_AUTHORITY)


def _retain(root, entry, value, suffix):
    from ouroboros.artifacts import store_actor_source_bytes
    ref = store_actor_source_bytes(root, entry.task_id, category="tool_results",
        source_id=f"delegated-{suffix}-{entry.run_id}",
        data=json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"), extension="json")
    return {**ref, "task_id": entry.task_id}


def _read_source(root, entry, ref):
    from ouroboros.artifacts import read_actor_source_bytes
    return json.loads(read_actor_source_bytes(root, str(ref.get("task_id") or entry.task_id), ref))


def observe_run(root, entry, detail: dict) -> bool:
    """Publish exact execution/question facts, never collection, control or consumption."""
    summary = c.summary_of(detail)
    if c.names_another_run(detail, entry.run_id):  # a run held by its job id is still this run
        raise ValueError("execution observation belongs to another run")
    state = str(summary.get("state") or "")
    seq = detail.get("lastSeq", 0)
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise ValueError("execution observation has no valid sequence")
    fresh = current.run(root, entry.run_id)
    if fresh is None or fresh.invocation_id != entry.invocation_id or fresh.task_id != entry.task_id:
        return False
    if seq < fresh.observation_seq:
        return False
    if state in c.TERMINAL_STATES:
        if fresh.terminal_state and fresh.terminal_state != state:
            return False
        source = _retain(root, fresh, detail, "terminal")
        if source.get("sha256") == fresh.terminal_source.get("sha256"):
            return False  # Retained revisions are immutable; changed source earns new bytes.
        return c.emit(root, c.TERMINAL_OBSERVED, {
            "run_id": fresh.run_id, "task_id": fresh.task_id, "root_task_id": fresh.root_task_id,
            "parent_task_id": fresh.parent_task_id, "invocation_id": fresh.invocation_id,
            "route": fresh.route_id, "source": fresh.source, "project_id": fresh.project_id,
            "state": state, "reason": str((summary.get("outcomeFacts") or {}).get("reason") or ""),
            "observation_seq": seq, "source_ref": source})
    if fresh.terminal_state:
        return False
    from ouroboros.gateways.claudexor import pending_interactions
    pending = pending_interactions(detail, strict=True)
    ids = [str(item["interaction_id"]) for item in pending]
    if not pending and not fresh.pending_interaction_ids:
        return False
    envelope = {"run_id": fresh.run_id, "pending_interactions": pending}
    source = _retain(root, fresh, envelope, "interactions")
    if source.get("sha256") == fresh.interaction_source.get("sha256"):
        return False
    return c.emit(root, c.INTERACTIONS_OBSERVED, {
        "run_id": fresh.run_id, "task_id": fresh.task_id, "observation_seq": seq,
        "source_ref": source, "interaction_ids": ids})


def _collection_failure(entry, step, exc, failures) -> bool:
    """A changed physical failure is loud; unchanged retries keep the same known debt."""
    signature = (entry.terminal_source.get("sha256"), type(exc).__name__,
                 getattr(exc, "code", None), getattr(exc, "errno", None), getattr(exc, "reason", None), str(exc))
    changed = failures is None or failures.get(step) != signature
    if failures is not None:
        failures[step] = signature
    log.log(logging.WARNING if changed else logging.DEBUG,
            "Delegated %s remains owed for %s: %s", step, entry.run_id, exc, exc_info=True)
    return changed


def record_terminal_money(root, entry, detail: dict, *, failures=None) -> dict:
    """The old exact session accounting, without project retirement or output ACK."""
    if entry.settled:
        return {"settled": True, "ledger_recorded": entry.ledger_recorded, "retried": False}
    from ouroboros.delegate_output import engine_locator
    from ouroboros.gateways.claudexor import final_attempt_facts, run_failure_cause
    from ouroboros.usage_accounting import record_subscription_session

    summary = c.summary_of(detail)
    if not c.is_terminal(detail):
        raise ValueError("cannot record terminal money for a live execution")
    observed = final_attempt_facts(detail, entry.run_id)
    spend, estimated = c.disclosed_spend(summary, attempt_execution=detail.get("attemptExecution"))
    profile, access = observed.get("profile_id", ""), str(summary.get("effectiveAccess") or "")
    if not entry.ledger_recorded:
        try:
            from ouroboros.install_identity import require_owned_root

            require_owned_root(entry.ledger_root or root)  # the usage store is not behind ``open_store``'s check
            attempt_id = record_subscription_session(entry.run_id, drive_root=pathlib.Path(entry.ledger_root or root),
                route=entry.route_id, model=observed.get("model", ""), task_id=entry.task_id,
                root_task_id=entry.root_task_id, parent_task_id=entry.parent_task_id,
                category=entry.category, source=entry.source,
                prompt_tokens=c.disclosed_tokens(summary.get("inputTokens")),
                completion_tokens=c.disclosed_tokens(summary.get("outputTokens")),
                cached_tokens=c.disclosed_tokens(summary.get("cachedInputTokens")),
                input_token_usage=summary.get("inputTokenUsage"), attempt_execution=detail.get("attemptExecution"),
                effort_resolution=observed.get("effort_resolution"), spend_usd=spend, spend_estimated=estimated,
                credential_profile_id=profile, access_profile=access,
                **{key: getattr(entry, key) for key in REVIEW_ATTRIBUTION_KEYS})
        except Exception as exc:
            if _collection_failure(entry, "money", exc, failures):
                c.emit(root, c.SETTLE_FAILED, {"run_id": entry.run_id, "task_id": entry.task_id,
                                             "route": entry.route_id, "step": "ledger_write"})
        else:
            if failures is not None:
                failures.pop("money", None)
            entry.ledger_recorded = True
            c.emit(root, c.LEDGER_RECORDED, {"run_id": entry.run_id, "task_id": entry.task_id,
                "root_task_id": entry.root_task_id, "parent_task_id": entry.parent_task_id, "route": entry.route_id})
            if not entry.review_owned:
                _emit_session_usage(root, entry, summary, observed, attempt_id)
    if entry.ledger_recorded:
        failure = summary.get("failure") if isinstance(summary.get("failure"), dict) else {}
        facts = summary.get("outcomeFacts") if isinstance(summary.get("outcomeFacts"), dict) else {}
        failed = {} if str(summary.get("state") or "") in c.SUCCEEDED_STATES else {
            "requested_model": entry.model, "failure_code": str(failure.get("code") or ""),
            "reported_cause": run_failure_cause(failure), "outcome_reason": str(facts.get("reason") or "")}
        entry.settled = c.emit(root, c.SETTLED, {
            "run_id": entry.run_id, "task_id": entry.task_id, "root_task_id": entry.root_task_id,
            "parent_task_id": entry.parent_task_id, "route": entry.route_id,
            "source": entry.source, "category": entry.category, "model": observed.get("model", ""),
            "observed_attempt": observed, "state": str(summary.get("state") or ""), **failed,
            "preserve_containment": True,
            "cost_usd": spend, "cost_final": spend is not None and not estimated,
            "spend_disclosed": spend is not None, "spend_estimated": estimated,
            "credential_profile_id": profile, "access_profile": access, "engine_locator": engine_locator(detail)})
        if entry.settled:
            entry.engine_locator = engine_locator(detail)
            try:
                from ouroboros.subagent_history import record_session_execution
                record_session_execution(root, entry, detail, observed)
            except Exception:
                log.debug("Session history unavailable", exc_info=True)
    return {"settled": entry.settled, "ledger_recorded": entry.ledger_recorded, "retried": True}


def _emit_session_usage(root, entry, summary, observed, attempt_id):
    """Best-effort telemetry after money; a missing event never repeats inference.

    The custody event already represents unknown/legacy sessions. Only a valid
    native split adds token facts. Recollection can repeat this row; consumers
    key it by run, while the ledger remains the sole monetary authority.
    """
    from ouroboros.review_session_usage import session_usage_facts
    from ouroboros.contracts.record_contract import RECORD_CONTRACT_VERSION
    from ouroboros.event_usage import event_token_counts
    from ouroboros.utils import append_jsonl, utc_now_iso

    facts = session_usage_facts(entry.run_id, requested_route=entry.route_id,
                                observed_attempt=observed, summary=summary)
    if "input_token_usage" not in facts:
        return
    try:
        append_jsonl(c.event_log_path(root), {
            "type": "llm_usage", "ts": utc_now_iso(), "record_contract_version": RECORD_CONTRACT_VERSION,
            "task_id": entry.task_id, "root_task_id": entry.root_task_id or entry.task_id,
            "parent_task_id": entry.parent_task_id, "delegation_role": "subagent" if entry.parent_task_id else "",
            "source": entry.source, "category": entry.category, "provider": "claudexor", "api_key_type": "claudexor",
            "model": "", "claudexor": facts["claudexor"], "input_token_usage": facts["input_token_usage"],
            **event_token_counts(facts),
            "accounting_authority": "physical_attempt_ledger", "ledger_attempt_ids": [attempt_id],
        })
    except Exception:
        log.warning("Delegated usage event unavailable for %s", entry.run_id, exc_info=True)


def _retain_output(root, entry, detail, gateway, staging_task_id="", output_authority=None):
    from ouroboros.delegate_output import retain_terminal_bytes, _resolve_full_primary_output
    from ouroboros.tools.delegate_terminal_evidence import resolved_terminal_payload

    full = retained_payload(root, entry, require_complete=False)
    if full is None:
        # A legacy row has no captured grant. Its existing foreground owner
        # supplies its known shape; body-only work keeps the recorded unknown.
        authority = _shape(entry) if entry.access or output_authority is None else output_authority
        full, complete, disclosure = resolved_terminal_payload(entry.run_id, detail, authority, entry, gateway)
    else:
        # The same observed terminal already has an exact presentation source.
        # Retrying its missing output does not re-derive its grant/containment.
        full["primary_output"], complete, disclosure = _resolve_full_primary_output(
            gateway, entry.run_id, full.get("primary_output"))
        full.pop("primary_output_full", None)
    if disclosure is not None:
        full["primary_output_full"] = disclosure
    text = json.dumps(full, ensure_ascii=False, indent=2)
    artifact = retain_terminal_bytes(root, staging_task_id or entry.task_id, entry.run_id, text, text.encode("utf-8"))
    if artifact is None:
        return {}
    if (entry.output_sha == artifact["sha256"] and entry.output_source == artifact["source"]
            and entry.output_complete == complete
            and entry.output_terminal_sha == entry.terminal_source.get("sha256")):
        return {"artifact": artifact, "complete": complete, "published": True}
    published = c.emit(root, c.OUTPUT_SPILLED, {"run_id": entry.run_id, "task_id": entry.task_id,
        "artifact": artifact["source"]["path"], "source": artifact["source"], "bytes": artifact["bytes"],
        "sha256": artifact["sha256"], "terminal_sha": entry.terminal_source.get("sha256", ""),
        "total_chars": len(text), "staged": True, "full_content": complete})
    return {"artifact": artifact, "complete": complete, "published": published}


def collect_terminal(root, run_id: str, *, gateway=None, detail: dict | None = None,
                     allow_review: bool = False, staging_task_id: str = "", stopped=None, failures=None,
                     output_authority=None) -> dict:
    """One result-owner entry for foreground collection and the body, including recovered debt.

    A contending caller reads committed facts; it never times out an integration
    or evicts a live collector. No queue/root/money lock is held at this entry.
    """
    from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock
    from ouroboros.delegate_continuation import disposition_refusal
    from ouroboros.tools.delegate_integration import capture_terminal_patch_for_drive
    from ouroboros.tools.subagent_integration_delegated import retain_run_evidence

    root = pathlib.Path(root)
    if stopped is not None and stopped():
        return {"status": "pending", "reason": "runtime_closing"}
    entry = current.run(root, run_id)
    if entry is None or (entry.review_owned and not allow_review):
        return {"status": "unavailable", "reason": "collection_owner_mismatch"}
    if detail is not None:
        observe_run(root, entry, detail)
    entry = current.run(root, run_id)
    if entry is None or not entry.terminal_source:
        return {"status": "pending", "reason": "terminal_not_observed"}
    lock = c.disposition_lock_path(root, entry)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = acquire_exclusive_file_lock(lock, timeout_sec=0, owner_aware_stale=True)
    if fd is None:
        return {"status": "pending", "reason": "collection_in_progress"}
    capture = None
    retention = {}
    errors = []
    try:
        entry = current.run(root, run_id)
        if entry is None:
            return {"status": "unavailable", "reason": "custody_missing"}
        detail = _read_source(root, entry, entry.terminal_source)
        if (not entry.output_source or not entry.output_complete
                or entry.output_terminal_sha != entry.terminal_source.get("sha256")):
            try:
                retention = _retain_output(root, entry, detail, gateway, staging_task_id, output_authority)
            except Exception as exc:
                errors.append(f"output:{type(exc).__name__}")
                _collection_failure(entry, "output", exc, failures)
            else:
                if failures is not None:
                    failures.pop("output", None)
        entry = current.run(root, run_id)
        # This lock is shared with continue/apply. A possibly-started successor
        # owns the mutable snapshot, even before it has a bound run id.
        if (entry is not None and not (stopped is not None and stopped())
                and not disposition_refusal(root, entry)):
            try:
                capture = capture_terminal_patch_for_drive(root, entry, gateway=gateway, retain_investigation=False)
                if isinstance(capture, dict) and capture.get("status") == "failed":  # a typed failure stays owed
                    raise RuntimeError(str(capture.get("note") or "patch capture failed"))
            except Exception as exc:
                errors.append(f"capture:{type(exc).__name__}")
                _collection_failure(entry, "capture", exc, failures)
            else:
                if failures is not None:
                    failures.pop("capture", None)
        entry = current.run(root, run_id)
        if entry is not None:
            closing = stopped is not None and stopped()
            money = ({"settled": entry.settled, "ledger_recorded": entry.ledger_recorded}
                     if closing else record_terminal_money(root, entry, detail, failures=failures))
            # Named evidence and evidence-only cleanup come AFTER full output
            # retention. Mutating results remain the author's decision.
            if not closing and capture and entry.output_complete and entry.output_source:
                capture = retain_run_evidence(root, entry, capture, gateway)
            if not closing and entry.project_persistent and entry.project_owned:
                c.retire_project(root, gateway, entry)  # records kept, never deletes the project
            return {"status": "collected" if not collection_pending(current.run(root, run_id), root) else "pending",
                    "settlement": money, "workspace_capture": capture, "output_retention": retention, "errors": errors}
        return {"status": "unavailable", "reason": "custody_missing"}
    finally:
        release_exclusive_file_lock(lock, fd)


def retained_payload(root, entry, *, require_complete: bool = True) -> dict | None:
    """Read verified canonical body bytes, without claiming a reader consumed them."""
    if (not entry.output_source or (require_complete and not entry.output_complete)
            or entry.terminal_source and entry.output_terminal_sha != entry.terminal_source.get("sha256")):
        return None
    try:
        return _read_source(root, entry, entry.output_source)
    except Exception:
        log.warning("Retained output unavailable for %s", entry.run_id, exc_info=True)
        return None


def saved_named_evidence(root, entry) -> tuple[pathlib.Path, dict]:
    """Where a Git run's retained ``.review-evidence/`` summary lives and what it last said (``{}``
    when none): one addressed read, never a recapture (#2070)."""
    path = c.delegated_capture_dir(root, entry.task_id, c.capture_key(entry)) / "evidence" / "summary.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return path, saved if isinstance(saved, dict) else {}


def capture_projection(root, entry, *, reader_task_id="") -> dict | None:
    """Existing captured manifest or an honest pending fact; never triggers capture.

    A Git run's saved named-evidence summary rides along, so saved-result and
    retry-successor delivery keep its status and reader."""
    directory = entry.resource_ref.get("workspace_kind") == "directory"
    if not entry.execution_root and not directory:
        return None
    cap_dir = c.delegated_capture_dir(root, entry.task_id, entry.run_id if directory else c.capture_key(entry))
    try:
        manifest = json.loads((cap_dir / "workspace_patch.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        from ouroboros.delegate_registration_policy import evidence_only

        if evidence_only(entry):  # no author decision is owed
            return {"status": "pending", "note": "Body capture is still owed; evidence only, never applied.",
                    "patch_disposition": entry.patch_disposed or "not_applicable"}
        return {"status": "pending", "note": "Body capture is still owed; no patch was applied. integrate_delegated_patch "
                "prepares a finished run's capture itself and returns it for inspection first.",
                "patch_disposition": entry.patch_disposed or "pending"}
    other_reader = bool(reader_task_id and reader_task_id != entry.task_id)
    if directory:
        from ouroboros.delegate_directory import directory_capture_block
        block = directory_capture_block(root, entry, manifest)
    else:
        from ouroboros.tools.delegate_integration import _capture_block
        block = _capture_block(entry, cap_dir, manifest, manifest.get("authority_drift") or {})
        summary, named = saved_named_evidence(root, entry)
        if named:  # another task reads it by absolute artifact path, as the patch and manifest below
            block["named_evidence"] = ({**named, "summary_read": {"root": "artifact_store", "path": str(summary)}}
                                       if other_reader and named.get("summary_read") else named)
    if other_reader:
        for kind in ("patch", "manifest"):
            if block.get(f"{kind}_read") and block.get(f"{kind}_artifact"):
                block[f"{kind}_read"] = {"root": "artifact_store", "path": block[f"{kind}_artifact"]}
    return block


_RUNTIMES: dict[str, "BodyRuntime"] = {}
_RUNTIME_LOCK = threading.RLock()


def _collection_facts(entry):
    return (entry.terminal_source.get("sha256"), entry.output_sha, entry.output_complete,
            entry.output_terminal_sha, entry.patch_captured, entry.patch_disposed, entry.superseded_by,
            entry.ledger_recorded, entry.settled)


class BodyRuntime:
    """One generation's owned physical jobs. No background job grants actor authority."""
    def __init__(self, root, *, stop_event=None, gateway_factory=None, max_workers=None, collection_ready=None):
        from ouroboros.config import get_max_workers
        from ouroboros.platform_layer import process_start_time

        self.root = pathlib.Path(root).resolve()
        self.external_stop = stop_event
        self.gateway_factory = gateway_factory
        self.collection_ready = collection_ready or (lambda: True)
        self.stop = threading.Event()
        self.changed = threading.Event()
        self.lock = threading.RLock()
        self.runtime_id = uuid.uuid4().hex
        self.birth = process_start_time(os.getpid())
        width = max(2, int(max_workers if max_workers is not None else get_max_workers()))
        self.observers = ThreadPoolExecutor(max_workers=width, thread_name_prefix="run-observe")
        self.collectors = ThreadPoolExecutor(max_workers=width, thread_name_prefix="run-collect")
        self.reads: dict[str, Future] = {}
        self.collections: dict[str, Future] = {}
        self.requested: set[str] = set()
        self.retry_requested: set[str] = set()
        self.collection_attempts: dict[str, tuple] = {}
        self.collection_failures: dict[str, dict] = {}
        # Each held run's latest dated execution fact, as the last observation read it.
        self.observed: dict[str, dict] = {}
        self.observation_gaps: dict[str, tuple] = {}
        self._observing = None
        self._connecting = None
        self.thread = threading.Thread(target=self._drive, name="delegated-body", daemon=True)
        try:
            self.thread.start()
        except BaseException:
            self.stop.set()
            self.observers.shutdown(wait=False, cancel_futures=True)
            self.collectors.shutdown(wait=False, cancel_futures=True)
            raise

    @property
    def active(self):
        return self.thread.is_alive() and not self.stop.is_set() and not (
            self.external_stop is not None and self.external_stop.is_set())

    def identity(self):
        return {"active": self.active, "runtime_id": self.runtime_id,
                "pid": os.getpid(), "process_birth": self.birth}

    def ensure_runs(self, pairs, *, task_id: str = "") -> bool:
        if not self.active:
            return False
        ids = []
        for rid, iid in pairs:
            entry = current.run(self.root, str(rid))
            if (entry is None or entry.review_owned or entry.invocation_id != str(iid)
                    or (task_id and entry.task_id != task_id)):
                return False
            ids.append(entry.run_id)
        with self.lock:
            if not self.active:
                return False
            self.requested.update(ids)
            self.changed.set()
        return True

    def request(self, run_id: str):
        with self.lock:
            if self.active:
                self.requested.add(str(run_id))
                self.retry_requested.add(str(run_id))
                self.observation_gaps.pop(str(run_id), None)
                self.changed.set()

    def _gateway(self):
        if self.gateway_factory is not None:
            return self.gateway_factory()
        from ouroboros.claudexor_daemon import read_owned_gateway
        return read_owned_gateway()

    def _observer_gateway(self):
        """One observation client; discovery and handshake never hold the runtime lock."""
        from ouroboros.gateways.claudexor_metadata import endpoint_generation

        with self.lock:
            gateway = self._observing
        endpoint = getattr(gateway, "_endpoint", None)
        if endpoint is not None and endpoint.descriptor_path and endpoint_generation(endpoint) is None:
            self._drop_observer_gateway(gateway)
        with self.lock:
            if self._observing is not None:
                return self._observing
            owner = self._connecting is None
            if owner:
                self._connecting = Future()
            pending = self._connecting
        if owner:
            try:
                gateway = self._gateway()
                with self.lock:
                    self._observing = gateway
                pending.set_result(gateway)
            except BaseException as exc:
                pending.set_exception(exc)
            finally:
                with self.lock:
                    self._connecting = None
        return pending.result()

    def _observation_key(self, entry):
        from ouroboros.gateways.claudexor_metadata import endpoint_generation

        gateway = self._observing
        endpoint = getattr(gateway, "_endpoint", None)
        generation = endpoint_generation(endpoint) if endpoint is not None else id(gateway)
        return generation, entry.invocation_id, entry.daemon_store_id

    def _drop_observer_gateway(self, gateway=None):
        with self.lock:
            if gateway is not None and self._observing is not gateway:
                return
            gateway, self._observing = self._observing, None
        if gateway is not None:
            gateway.close()

    def _observe(self, entry):
        from ouroboros.gateways.claudexor import SHORT_POLL_TIMEOUT_SEC
        gateway = self._observer_gateway()
        try:
            detail = gateway.get_run(entry.run_id, timeout_sec=SHORT_POLL_TIMEOUT_SEC)
        except BaseException as exc:
            self._remember(entry, None, error=exc)
            if c.daemon_says_absent(exc, gateway=gateway, run=entry):
                c.close_absent_run(self.root, gateway, entry, "body_observation_absent")
                return
            if c.daemon_says_absent(exc):
                c.record_containment_fault(self.root, entry, "run_absence_identity_unproven",
                    "The daemon returned 404, but ownership by this store is unproven; run outcome remains unknown.")
                with self.lock:
                    self.observation_gaps[entry.run_id] = self._observation_key(entry)
            if not getattr(exc, "status_code", 0) and not getattr(exc, "observation_answered", False):
                self._drop_observer_gateway(gateway)
            raise
        with self.lock:
            self.observation_gaps.pop(entry.run_id, None)
        # A late valid source fact remains evidence; it grants no wait.
        observe_run(self.root, entry, detail)
        c.bind_daemon_store(self.root, gateway, entry)
        if entry.containment_fault.get("reason") == "run_absence_identity_unproven":
            c.resolve_containment_fault(self.root, entry, "run_observed",
                                        expected_reason="run_absence_identity_unproven")
        self._remember(entry, detail)

    def _remember(self, entry, detail, *, error=None):
        """Keep this read's execution fact for readers that must not poll the engine themselves
        (``gateway/reply_preparation.py``), including a received HTTP refusal."""
        from ouroboros.delegate_supervision import run_activity

        summary = c.summary_of(detail) if detail is not None else {}
        fact = {"run_id": entry.run_id, "task_id": entry.task_id, "invocation_id": entry.invocation_id,
                "answered": detail is not None or bool(getattr(error, "status_code", 0)),
                "error_code": str(getattr(error, "code", "")), "http_status": getattr(error, "status_code", 0),
                "run_state": str(summary.get("state") or ""),
                "waiting_on_user": bool(summary.get("waitingOnUser") or (detail or {}).get("pendingInteractions")),
                "at": time.monotonic()}  # dated on this process's clock, like every in-process reader
        with self.lock:
            previous, self.observed[entry.run_id] = self.observed.get(entry.run_id), fact
        if run_activity(fact) is True and (previous is None or run_activity(previous) is not True):
            _announce_working(entry)  # first seen working too: a restarted host's idle cue learns of it

    def observation(self, run_id) -> dict:
        with self.lock:
            return dict(self.observed.get(str(run_id)) or {})

    def _collect(self, run_id):
        try:
            gateway = self._gateway()
        except Exception:
            gateway = None  # Local retained output, Git capture and money still work.
        try:
            with self.lock:
                failures = self.collection_failures.setdefault(run_id, {})
            return collect_terminal(self.root, run_id, gateway=gateway, stopped=lambda: not self.active,
                                    failures=failures)
        finally:
            if gateway is not None:
                gateway.close()

    def tick(self):
        from ouroboros.config import BODY_MAINTENANCE_INTERVAL_SEC
        if not self.active:
            return
        entries = current.runs(self.root, "state=?", (current.HELD,))
        with self.lock:
            for jobs in (self.reads, self.collections):
                for rid, future in list(jobs.items()):
                    if future.done():
                        if not future.cancelled() and future.exception() is not None:
                            log.debug("Body operation for %s remains owed: %s", rid, future.exception())
                        del jobs[rid]  # Completed history is not an in-flight registry.
                        if jobs is self.collections and rid in self.collection_attempts:
                            facts, _when = self.collection_attempts[rid]
                            self.collection_attempts[rid] = (facts, time.monotonic())
            requested, self.requested = self.requested, set()
        for rid in requested - entries.keys():
            entry = current.run(self.root, rid)
            if entry is not None:
                entries[rid] = entry
        with self.lock:
            for rid in set(self.observed) - set(entries):
                del self.observed[rid]  # a run no longer held has no current execution fact
                self.observation_gaps.pop(rid, None)
        with self.lock:
            self.retry_requested.intersection_update(
                rid for rid in self.retry_requested
                if rid in self.collections or collection_pending(entries.get(rid), self.root))
            for rid in list(self.collection_attempts):
                if rid not in self.collections and not collection_pending(entries.get(rid), self.root):
                    self.collection_attempts.pop(rid, None)
                    self.collection_failures.pop(rid, None)
                    self.retry_requested.discard(rid)
        for entry in entries.values():
            if entry.review_owned or not self.active:
                continue
            with self.lock:
                if not entry.terminal_source and (not entry.settled or entry.terminal_state):
                    previous = self.reads.get(entry.run_id)
                    if (previous is None or previous.done()) and self.observation_gaps.get(entry.run_id) != self._observation_key(entry):
                        self.reads[entry.run_id] = self.observers.submit(self._observe, entry)
                if self.collection_ready() and collection_pending(entry, self.root):
                    previous = self.collections.get(entry.run_id)
                    facts, now = _collection_facts(entry), time.monotonic()
                    old, ended = self.collection_attempts.get(entry.run_id, (None, 0.0))
                    due = (old != facts or entry.run_id in self.retry_requested
                           or now - ended >= BODY_MAINTENANCE_INTERVAL_SEC)
                    if (previous is None or previous.done()) and due:
                        self.retry_requested.discard(entry.run_id)
                        self.collection_attempts[entry.run_id] = (facts, now)
                        self.collections[entry.run_id] = self.collectors.submit(self._collect, entry.run_id)

    def _drive(self):
        from ouroboros.delegate_supervision import _TICK_SEC
        try:
            while not self.stop.is_set() and not (self.external_stop is not None and self.external_stop.is_set()):
                try:
                    self.tick()
                except Exception:
                    log.warning("Body run observation unavailable; custody retained", exc_info=True)
                self.changed.wait(_TICK_SEC)
                self.changed.clear()
        finally:
            self.stop.set()
            # Actual jobs remain owned until their physical return; a caller of
            # close(wait=False) neither joins on the control loop nor forgets them.
            self.observers.shutdown(wait=True, cancel_futures=True)
            self.collectors.shutdown(wait=True, cancel_futures=True)
            self._drop_observer_gateway()
            with _RUNTIME_LOCK:
                if _RUNTIMES.get(str(self.root)) is self:
                    del _RUNTIMES[str(self.root)]

    def close(self, *, wait=False):
        self.stop.set()
        self.changed.set()
        if wait and threading.current_thread() is not self.thread:
            self.thread.join()


def _announce_working(entry) -> None:
    """A held run works now and did not at its last read (or had none): say so the way a
    resumed attempt does, since its task may be a sleeper whose Telegram typing had stopped
    (DESIGN §9). Best effort: a lost frame costs one missed cue."""
    from types import SimpleNamespace

    try:
        from ouroboros.model_sleep import announce_resumed
        from supervisor import queue as q
        from supervisor.active_activity import get_direct_activity_registry
        from supervisor.workers import get_event_q

        with q._queue_lock:
            rows = [meta.get("task") for meta in q.RUNNING.values() if isinstance(meta, dict)] + list(q.PENDING)
        task = next((row for row in rows if isinstance(row, dict) and str(row.get("id") or "") == entry.task_id), None)
        turn = get_direct_activity_registry().get(entry.task_id) if task is None else None
        if task is None and turn is None:
            return
        announce_resumed(SimpleNamespace(event_queue=get_event_q(create=False), task_id=entry.task_id,
                                         current_chat_id=task.get("chat_id") if task is not None else turn.chat_id))
    except Exception:
        log.debug("Working-again announce unavailable for %s", entry.run_id, exc_info=True)


def current_activity(root, task_id: str, run_id: str, invocation_id: str = ""):
    """Whether a held run works now by this observer's latest read: the same run, task and
    (when given) invocation, read within a few ticks; anything else is None (unknown)."""
    from ouroboros.delegate_supervision import _OBSERVATION_CURRENT_SEC, run_activity

    runtime = body_runtime(root)
    fact = runtime.observation(run_id) if runtime is not None else {}
    try:
        current = "at" in fact and time.monotonic() - float(fact["at"]) <= _OBSERVATION_CURRENT_SEC
    except (TypeError, ValueError):
        current = False
    if (not current or fact.get("task_id") != str(task_id)
            or (invocation_id and fact.get("invocation_id") != str(invocation_id))):
        return None
    return run_activity(fact)


def start_body_runtime(root, *, stop_event=None, gateway_factory=None, max_workers=None, collection_ready=None) -> BodyRuntime:
    key = str(pathlib.Path(root).resolve())
    with _RUNTIME_LOCK:
        existing = _RUNTIMES.get(key)
        if existing is not None:
            return existing  # A closing generation is not replaced while its jobs remain.
        runtime = BodyRuntime(key, stop_event=stop_event, gateway_factory=gateway_factory, max_workers=max_workers,
                              collection_ready=collection_ready)
        _RUNTIMES[key] = runtime
        return runtime


def body_runtime(root) -> BodyRuntime | None:
    with _RUNTIME_LOCK:
        runtime = _RUNTIMES.get(str(pathlib.Path(root).resolve()))
        return runtime if runtime is not None and runtime.active else None


def stop_body_runtime(root, *, wait=False) -> None:
    with _RUNTIME_LOCK:
        runtime = _RUNTIMES.get(str(pathlib.Path(root).resolve()))
    if runtime is not None:
        runtime.close(wait=wait)
