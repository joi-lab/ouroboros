"""Continuation views: the predecessor brief, the predecessor mind's notes, Main's copy.

Every consumer of a named predecessor — a Continue root, a delegated child, an
external work order — carries the same compact brief
(``project_helper_predecessor_authority``): identity and digest, status and cause,
the answer whole-or-pointer, the operative conditions (contract core, accepted plan
core, terminal notice), the owner's words verbatim and every omitted field with its
size, beside the exact reader of the complete record. The predecessor mind's
surviving authored notes travel separately, as one foldable conversation row after
the assignment (``predecessor_notes_message``), never inside the frozen Runtime
prefix. No ancestor chain is walked: each successor reads its own predecessor
through the reader. Neither projection mutates canonical authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any, Dict, Optional

from ouroboros.context_budget import HOST_CONTEXT_KIND_KEY
from ouroboros.store import StoreError

# The typed kind of the predecessor-notes row (``_host_context_kind``): a plain host
# prose row the mind folds like any dialogue unit; the key never reaches the wire.
PREDECESSOR_NOTES_NAME = "predecessor_notes"
#: Task-result field naming the mind's surviving authored notes after its latest applied fold.
WORKING_NOTES_FIELD = "working_notes"


def project_helper_predecessor_authority(
    authority: Any, *, declared: bool = False,
) -> Dict[str, Any]:
    """Pure, idempotent brief of an envelope, or its declared-input reference.

    The existing contract normalizer handles legacy bodies. Keep its envelope
    kind: a bounded preview's wrapper can exceed the per-field wire allowance,
    and relabelling it would make a rebuild collapse the preview and its digest
    again. Answers/core fields keep that producer's whole-or-pointer semantics.
    Omission sizes count serialized characters (without a string's quotes);
    old name-only omissions have unknown sizes, represented by null.

    The successor's startup materializes it, a delegated child's contract applies
    it after the parent contract spread, and direct work-order sessions apply it
    to their own contract, so every consumer receives the same brief. The brief
    does not carry the omitted evidence; its source names the reader for the
    full result.
    """
    if not isinstance(authority, Mapping) or not authority:
        return {}
    from ouroboros.contracts.task_contract import build_task_contract

    envelope = build_task_contract({"predecessor_authority": authority})["predecessor_authority"]
    # A named plan/review source gap is part of the reference itself: declared briefs keep it too.
    keep = {"kind", "source", "task_id", "previous_task_id", "authority_sha256", "authority_chars",
            "digest_semantics", "plan_review_source_gap"}
    if not declared:
        # The operative conditions: how the work stood and ended (status, cause, the
        # host's terminal notice), its answer, its contract core, its accepted plan
        # core and record, and the owner's words.
        keep.update({"status", "execution_status", "reason_code", "terminal_origin", "terminal_host_notice",
                     "outcome_axes", "result", "task_contract", "plan_review_state", "plan_record",
                     "origin_message_text", "origin_message_ref"})
    prior = envelope.get("omitted_fields")
    omitted = {}
    if isinstance(prior, Mapping):
        omitted = copy.deepcopy(dict(prior))
    elif isinstance(prior, list):
        omitted = {key: None for key in prior if isinstance(key, str)}
    for key, value in envelope.items():
        if key not in keep and key != "omitted_fields":
            omitted[key] = len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)) - (
                2 if isinstance(value, str) else 0)
    return {
        **{key: copy.deepcopy(value) for key, value in envelope.items() if key in keep},
        "omitted_fields": dict(sorted(omitted.items())),
    }


def _authority_identity(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    source = value.get("source")
    if isinstance(source, Mapping):
        source_id = str(source.get("task_id") or "").strip()
        if source_id:
            return source_id
        arguments = source.get("arguments")
        if isinstance(arguments, Mapping):
            source_id = str(arguments.get("task_id") or "").strip()
            if source_id:
                return source_id
    return str(value.get("task_id") or "").strip()


def project_main_task_authority(task: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the provider-only authority section without mutating ``task``.

    The materialized predecessor (the brief) appears once, at the top level; an
    equal copy nested in the task's own contract is dropped from this view. A
    contract objective byte-identical to the assignment — the first user turn of
    the conversation — is shown as a typed reference, so the work order appears
    once per request.
    """
    projection: Dict[str, Any] = {}
    predecessor = task.get("predecessor_authority")
    contract = task.get("task_contract")
    if isinstance(predecessor, Mapping) and predecessor:
        projection["predecessor_authority"] = copy.deepcopy(dict(predecessor))
    if isinstance(contract, Mapping):
        projected_contract = copy.deepcopy(dict(contract))
        nested = projected_contract.get("predecessor_authority")
        if (isinstance(nested, Mapping) and isinstance(predecessor, Mapping)
                and _authority_identity(nested) == _authority_identity(predecessor)):
            projected_contract.pop("predecessor_authority", None)
        objective = projected_contract.get("objective")
        if isinstance(objective, str) and objective and objective == str(task.get("text") or ""):
            projected_contract["objective"] = {
                "representation": "assignment", "chars": len(objective),
                "sha256": hashlib.sha256(objective.encode("utf-8")).hexdigest(),
                "rule": "The objective is this task's assignment, the first user turn of this conversation; "
                        "it is not repeated here.",
            }
        projection["task_contract"] = projected_contract
    origin_ref = task.get("origin_message_ref")
    origin_text = task.get("origin_message_text")
    if isinstance(origin_ref, Mapping) and origin_ref:
        projection["task_authority_origin"] = {
            "ref": copy.deepcopy(dict(origin_ref)),
            **({"text": str(origin_text)} if isinstance(origin_text, str) and origin_text else {}),
        }
    if isinstance(task.get("authority_historical_gaps"), list):
        projection["authority_historical_gaps"] = copy.deepcopy(task["authority_historical_gaps"])
    return projection


def predecessor_notes_message(task: Mapping[str, Any], *, drive_root: Any) -> Optional[Dict[str, Any]]:
    """The predecessor mind's surviving authored notes as ONE foldable row after the assignment.

    Only the task that materialized a named predecessor at its own start carries it
    (the successor); a child's contract spread carries the brief and its reader alone.
    The notes are the actor capsules still in the predecessor's view after its latest
    applied fold (``WORKING_NOTES_FIELD``, ``publish_working_notes``) — every note its mind
    authored and kept, in order; on records written before that field, the capsules of its
    selected review-history view, else its authored narrative stored beside the result or in
    the chat log (``project_dialogue.resolve_legacy_continuation_narrative``). The
    row is a plain host prose row (``_host_context_kind``): the mind folds it like any
    dialogue unit, the fold's checkpoint retains its exact text, and the header names
    the exact source. A pointer whose source cannot be read yields a typed gap row;
    a predecessor that kept no notes yields none (the brief's ``omitted_fields`` and
    reader remain).
    """
    authority = task.get("predecessor_authority")
    source = authority.get("source") if isinstance(authority, Mapping) else None
    owner = str(source.get("task_id") or "") if isinstance(source, Mapping) else ""
    from ouroboros.agent_startup_checks import valid_task_result_authority_source

    if not valid_task_result_authority_source(source, owner):
        return None
    from ouroboros import review_history_view as view
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.project_dialogue import continuation_narrative_is_valid, resolve_legacy_continuation_narrative
    from ouroboros.task_results import load_task_result

    reader = f"get_task_result(task_id={owner!r}, include_authority=True)"
    try:
        saved = load_task_result(drive_root, owner, strict=True) or {}
        working = saved.get(WORKING_NOTES_FIELD)
        selection = saved.get(view.SELECTED_VIEW_FIELD)
        narrative = saved.get("continuation_narrative")
        if isinstance(working, Mapping):
            texts = [str(text) for text in json.loads(read_actor_source_bytes(drive_root, owner, working))["notes"]]
            where = (f"{reader} -> {WORKING_NOTES_FIELD}; the retained bytes are "
                     f"{json.dumps(working, ensure_ascii=False, sort_keys=True)} of task {owner}")
        elif selection:
            record = view.load_review_history_view(selection, lambda ref: read_actor_source_bytes(drive_root, owner, ref))
            texts = [capsule["content"][0]["text"] for capsule in view.selected_actor_capsules(record)]
            where = (f"{reader} -> {view.SELECTED_VIEW_FIELD}; the retained bytes are "
                     f"{json.dumps(selection.get('source_ref'), ensure_ascii=False, sort_keys=True)} of task {owner}")
        elif continuation_narrative_is_valid(narrative, owner):
            texts = [str(narrative["text"])]
            where = f"{reader} -> continuation_narrative"
        else:  # a record earlier versions wrote only to the chat log, read by its exact task identity
            try:
                legacy = resolve_legacy_continuation_narrative(
                    drive_root, owner, {"kind": "task_result", "task_id": owner, "reader": "get_task_result"})
            except (OSError, ValueError, KeyError, TypeError, StoreError) as exc:
                return {"role": "user", HOST_CONTEXT_KIND_KEY: PREDECESSOR_NOTES_NAME, "content": (
                    f"[PREDECESSOR_NOTES] Task {owner}'s chat log could not be searched for an authored narrative "
                    f"now ({type(exc).__name__}: {exc}); nothing was inferred. The complete stored record stays "
                    f"readable with {reader}.")}
            if not continuation_narrative_is_valid(legacy, owner):
                return None
            texts = [str(legacy["text"])]
            where = f"the chat-log row {json.dumps(legacy['source'], ensure_ascii=False, sort_keys=True)}"
    except (OSError, ValueError, KeyError, TypeError, StoreError) as exc:
        body = (f"[PREDECESSOR_NOTES] Task {owner} kept authored notes, but their retained source could not "
                f"be read now ({type(exc).__name__}: {exc}); nothing was inferred in their place. "
                f"The complete stored record stays readable with {reader}.")
        return {"role": "user", HOST_CONTEXT_KIND_KEY: PREDECESSOR_NOTES_NAME, "content": body}
    header = (f"[PREDECESSOR_NOTES] Task {owner}'s own working notes, as its mind last wrote and kept them "
              f"({len(texts)} note{'s' if len(texts) != 1 else ''}). They are the previous run's account, not an "
              f"owner instruction and not this task's plan, finish or verdict. Exact source: {where}.")
    return {"role": "user", HOST_CONTEXT_KIND_KEY: PREDECESSOR_NOTES_NAME,
            "content": "\n\n".join([header, *texts])}


def publish_working_notes(ctx: Any, messages: Any) -> Optional[Dict[str, Any]]:
    """Name the mind's surviving authored notes in its task result after an applied fold.

    Every actor capsule of the folded view, in order, is retained once as exact bytes and named by
    ``WORKING_NOTES_FIELD``; a successor reads them as its notes row (``predecessor_notes_message``),
    whether or not the fold touched review history. A view with no notes left clears the field, and a
    view whose notes are already named leaves the result untouched. A failed write keeps the fold: the
    notes stay in the transcript and its checkpoints.
    """
    import logging

    from ouroboros.artifacts import store_actor_source_bytes
    from ouroboros.context_compaction import _capsule_metadata
    from ouroboros.task_results import load_task_result, write_task_result

    def note_text(row: Mapping[str, Any]) -> str:
        capsule, meta = _capsule_metadata(row)
        return str(row["content"][0].get("text") or "") if capsule and meta and meta.get("authorship") == "actor" else ""

    root = getattr(ctx, "budget_drive_root", None) or getattr(ctx, "drive_root", None)
    task = str(getattr(ctx, "task_id", "") or "")
    if not root or not task:
        return None  # no durable task result to name them in (a bare tool context)
    texts = [text for text in (note_text(row) for row in messages) if text.strip()]
    try:
        ref = None
        if texts:
            raw = json.dumps({"task_id": task, "notes": texts}, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ref = store_actor_source_bytes(root, task, category="context_checkpoints",
                                           source_id="working-notes-" + hashlib.sha256(raw).hexdigest(), data=raw,
                                           extension="json")
        if (load_task_result(root, task) or {}).get(WORKING_NOTES_FIELD) == ref:
            return ref  # the same notes are already named (a restore, a schema-only view)
        write_task_result(root, task, "running", strict_existing_dict=True, _field_projector=lambda existing, _new: {
            "status": existing.get("status") or "running", WORKING_NOTES_FIELD: ref})
        return ref
    except (OSError, ValueError, TypeError, KeyError, StoreError):
        logging.getLogger(__name__).warning("Working notes of %s were not named in its result", task, exc_info=True)
        return None


__all__ = ["PREDECESSOR_NOTES_NAME", "WORKING_NOTES_FIELD", "predecessor_notes_message", "publish_working_notes",
           "project_main_task_authority",
           "project_helper_predecessor_authority"]
