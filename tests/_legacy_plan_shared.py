"""Earlier-schema (v1/v2) plan-review records as finished installs still hold them.

The retired engine that wrote this shape is gone; what stays live reads it
(``task_results.load_plan_review_state``, ``plan_record.import_legacy_v2``,
``plan_review_artifacts.plan_review_dispute_history``, the acceptance binder). These
helpers plant such records directly, in the stored shape, so those readers stay covered.
Plain callables, never fixtures.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ouroboros.artifacts import store_actor_source_bytes
from ouroboros.task_result_schema import SCHEMA_VERSION_KEY, TASK_RESULT_SCHEMA_VERSION
from ouroboros.task_store import patch_document
from ouroboros.tools.plan_spec import spec_hash

DECK_SPEC: Dict[str, Any] = {
    "goal": "Ship the deck", "in_scope": ["slides"], "non_goals": [], "invariants": [],
    "decisions": [], "deferred": [], "evidence": [], "affected_resources": [], "affected_paths": [],
    "acceptance_claims": [{"id": "claim_1", "claim": "The deck has five slides", "surface": "", "support": "count them",
                           "priority": "must"}],
}


def persist_wave(root: Any, task_id: str, wave: Dict[str, Any]) -> Dict[str, Any]:
    """The wave's exact bytes in the task's artifact store, where (and how) the retired engine kept
    them: with its artifact metadata, through the observability redaction."""
    from ouroboros.observability import redact_projection

    fingerprint = str(wave.get("request_fingerprint") or "")
    payload = redact_projection({**wave, "artifact_meta": {
        "kind": "plan_review_wave", "schema_version": 1, "producer_task_id": task_id, "producer_root": "artifact_store",
        "source_generation": int(wave.get("cycle_index") or 0), "created_at": "2026-10-01T00:00:00+00:00",
        "read_operation": "read_file", "retention_owner": "task_artifact_store"}}).value
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return store_actor_source_bytes(
        root, task_id, category="context_checkpoints",
        source_id=f"plan-review-wave-{int(wave.get('cycle_index') or 0):04d}-{fingerprint}", data=data, extension="json")


def wave(cycle: int = 1, *, fingerprint: Optional[str] = None, spec: Optional[Dict[str, Any]] = None,
         **changes: Any) -> Dict[str, Any]:
    """One full v2 wave (no artifact yet): an open REVIEW_REQUIRED wave unless ``changes`` say otherwise."""
    spec = dict(spec or DECK_SPEC)
    return {"cycle_index": cycle, "request_fingerprint": fingerprint or f"{cycle:064x}", "spec": spec,
            "spec_hash": spec_hash(spec), "aggregate": "REVIEW_REQUIRED", "closed": False, "paid": True,
            "findings": [], "dispositions": [], "actors": [], "reviewer_outputs": [], **changes}


def with_artifact(root: Any, task_id: str, value: Dict[str, Any]) -> Dict[str, Any]:
    """``value`` with its exact copy retained and named by ``wave_artifact``."""
    return {**value, "wave_artifact": persist_wave(root, task_id, value)}


def v2_state(*waves: Dict[str, Any], current: Optional[str] = None, status: str = "open",
             reason: str = "", **extra: Any) -> Dict[str, Any]:
    """A v2 ``plan_review_state`` whose current attempt names ``current`` (default: the last wave)."""
    fingerprint = current if current is not None else (str(waves[-1]["request_fingerprint"]) if waves else "")
    return {"schema_version": 2, "series_id": (fingerprint or "0" * 16)[:16],
            "cycles_paid": sum(1 for w in waves if w.get("paid")), "need_evidence_seen": [],
            "current_attempt": {"fingerprint": fingerprint, "status": status, "reason": reason} if fingerprint else {},
            "waves": list(waves), "waves_omitted": 0, **extra}


def seed_plan_review_state(root: Any, task_id: str, state: Dict[str, Any], **document: Any) -> Dict[str, Any]:
    """Plant ``state`` under the task result's ``plan_review_state`` key verbatim (the rest of the
    document is kept; ``document`` sets other fields)."""
    def merge(current: Dict[str, Any]) -> Dict[str, Any]:
        body = {**dict(current or {}), **document}
        body.setdefault("task_id", str(task_id))
        body.setdefault("status", "running")
        body.setdefault(SCHEMA_VERSION_KEY, TASK_RESULT_SCHEMA_VERSION)
        body["plan_review_state"] = state
        return body

    return patch_document(root, task_id, merge)


def record_exact_wave(root: Any, task_id: str, value: Dict[str, Any]) -> Dict[str, Any]:
    """One wave as the retired engine's exact-wave writer left it: the operative spec retained
    as its own source (``plan-spec``), the whole wave retained as ``wave_artifact``, and a
    bounded hot index (``spec`` emptied, ``spec_in_artifact``) as the task's only wave.
    Returns the stored ``plan_review_state``."""
    hot = dict(value)
    hot["spec_source_ref"] = store_actor_source_bytes(
        root, task_id, category="context_checkpoints", source_id="plan-spec", extension="json",
        data=json.dumps(hot["spec"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    hot.update(author_request_fingerprint="", dialogue_chat_id=None)
    hot["wave_artifact"] = persist_wave(root, task_id, hot)
    hot = {**hot, "spec": {}, "spec_in_artifact": True}
    hot.pop("goal", None)
    hot.pop("evidence_manifest", None)
    state = v2_state(hot)
    seed_plan_review_state(root, task_id, state)
    return state


def seed_submitted_subject(root: Any, task_id: str, *, fingerprint: str, spec: Dict[str, Any],
                           plan_prose: str) -> Dict[str, Any]:
    """An attempt whose plan was submitted but reached no reviewer: the exact submitted subject
    retained as a ``plan_submitted_subject`` source and named by ``current_attempt``."""
    ref = store_actor_source_bytes(
        root, task_id, category="context_checkpoints", source_id=f"plan-author-{fingerprint}", extension="json",
        data=json.dumps({"kind": "plan_submitted_subject", "fingerprint": fingerprint, "spec": spec,
                         "plan_prose": plan_prose}, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    state = v2_state(current=fingerprint)
    state["current_attempt"]["submitted_subject"] = ref
    seed_plan_review_state(root, task_id, state)
    return state


# ------------------------------------------------------------------ recorded legacy records
#
# Golden fixtures under ``tests/fixtures/legacy_plan_dispute/``: the exact records the retired
# engine wrote for the flows the review-history view tests start from, recorded while it
# still existed. One gzip-compressed JSON file (``<name>.json.gz``) per starting point: the
# stored task-result document, every artifact-store file (exact bytes; a digest-named source
# handle is checked against its name), the workspace files the flow attached as evidence,
# and the facts the tests read back (``meta``). A fixture with a ``base`` holds only what
# its step added to that base. Compressed because the engine's wave artifacts keep every
# reviewer request it sent (megabytes of near-identical text that the readers verify by
# digest, so they stay byte-exact).

def _recorded(name: str) -> Dict[str, Any]:
    import gzip
    import pathlib

    path = pathlib.Path(__file__).resolve().parent / "fixtures" / "legacy_plan_dispute" / f"{name}.json.gz"
    value = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    if value.get("fixture") != "legacy_plan_dispute/1" or value.get("name") != name:
        raise ValueError(f"not a recorded legacy plan fixture: {path}")
    return value


def recorded_meta(name: str) -> Dict[str, Any]:
    """The recorded facts of fixture ``name`` and of every fixture it extends (newest wins)."""
    fixture = _recorded(name)
    return {**(recorded_meta(fixture["base"]) if fixture.get("base") else {}), **fixture.get("meta", {})}


def _plant_recorded_files(root: Any, fixture: Dict[str, Any], workspace: Any) -> None:
    import base64
    import pathlib
    import re
    from hashlib import sha256

    from ouroboros.artifacts import task_artifact_dir_path

    directory = task_artifact_dir_path(root, fixture["task_id"], create=True)
    files = {**{path: text.encode("utf-8") for path, text in fixture.get("artifacts", {}).items()},
             **{path: base64.b64decode(data) for path, data in fixture.get("binary_artifacts", {}).items()}}
    for relative, data in files.items():
        named = re.search(r"-([0-9a-f]{64})\.[A-Za-z0-9]+$", relative)
        if named and sha256(data).hexdigest() != named.group(1):
            raise ValueError(f"recorded artifact {relative} does not match the digest in its name")
        target = directory.joinpath(*pathlib.PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for relative in fixture.get("removed_artifacts", []):
        directory.joinpath(*pathlib.PurePosixPath(relative).parts).unlink(missing_ok=True)
    if workspace is not None:
        for relative, text in fixture.get("workspace", {}).items():
            target = pathlib.Path(workspace).joinpath(*pathlib.PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))


def plant_recorded(root: Any, name: str, *, workspace: Any = None) -> Dict[str, Any]:
    """Plant recorded fixture ``name`` (after the fixtures it extends) into the fresh data root
    ``root``: the task-result document exactly as stored, the artifact files, and, when
    ``workspace`` is given, the workspace files the flow attached. Returns ``recorded_meta``."""
    fixture = _recorded(name)
    if fixture.get("base"):
        plant_recorded(root, fixture["base"], workspace=workspace)
    _plant_recorded_files(root, fixture, workspace)
    document = json.loads(json.dumps(fixture["document"]))
    patch_document(root, fixture["task_id"], lambda _current: document)
    return recorded_meta(name)


def advance_recorded(root: Any, name: str, *, workspace: Any = None) -> Dict[str, Any]:
    """The retired engine's next recorded step ``name`` over a data root that already holds the
    step's base (planted earlier in the same test): its new artifact files, and the document
    keys the step changed. What the test itself wrote meanwhile (a selected review view, a
    lifecycle status, the write stamps) stays. Returns ``recorded_meta``."""
    fixture = _recorded(name)
    before = _recorded(fixture["base"])["document"]
    _plant_recorded_files(root, fixture, workspace)
    stamps = {"ts", "updated_at"}
    changed = {key: value for key, value in fixture["document"].items() if key not in stamps and before.get(key) != value}
    removed = {key for key in before if key not in fixture["document"] and key not in stamps}
    changed = json.loads(json.dumps(changed))

    def merge(current: Dict[str, Any]) -> Dict[str, Any]:
        return {**{key: value for key, value in dict(current or {}).items() if key not in removed}, **changed}

    patch_document(root, fixture["task_id"], merge)
    return recorded_meta(name)
