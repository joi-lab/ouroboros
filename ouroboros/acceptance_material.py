"""Retain produced material before root-result admission, then reuse it in review.

Inputs, checkpoints and requested outputs do not establish production. These
sources use the existing artifact/source owners; this module neither dispatches
critics nor writes a terminal/quiescent mutation state. A failed capture stays a
gap and never resurrects the previous selection.

Material names the execution whose store holds its sources (``task_id``). A retry
under a new execution id restores that record with the work it continues, so the
work's sources stay its premises: what the retry does not produce itself is copied
into its own store under the same content references (``_carry_continued_sources``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_WORKSPACE_SOURCES = ("produced-workspace", "produced-workspace-diff")
_PRODUCED_FILES = "source_handles/context_checkpoints/produced-"


def retain_acceptance_material(ctx: Any, trace: dict, content: str | None = None, *, include_outputs: bool = True) -> dict:
    from ouroboros.artifacts import (
        collect_task_artifact_records, copy_artifact_file, persist_exact_text_source,
        registered_task_artifact, stream_artifact_file,
        task_artifact_dir_path, read_actor_source_bytes,
    )
    from ouroboros.contracts.task_contract import answer_protocol_active
    from ouroboros.outcomes import selected_final_answer
    from ouroboros.artifacts import materialize_tool_result_source
    from ouroboros.extension_surface_names import parse_extension_surface_name
    from ouroboros.review_evidence import _accept_protected_set, _accept_repo_diff

    previous = trace.get("acceptance_material") or {}
    material: dict = {"produced": False, "sources": [], "issues": []}
    attempted: set = set()  # names this execution produces itself (or failed to): never carried
    unknown: set = set()  # kinds whose own capture failed: nothing of that kind is carried
    trace["acceptance_material"] = material
    root, task_id = getattr(ctx, "drive_root", None), str(getattr(ctx, "task_id", "") or "")
    if root is None or not task_id:
        return material
    material["task_id"] = task_id

    def retain(name: str, text: str) -> dict:
        attempted.add(name)
        prior = next((row.get("source_ref") for row in previous.get("sources", []) if row.get("name") == name), None)
        try:
            reusable = bool(prior) and read_actor_source_bytes(root, task_id, prior) == text.encode("utf-8")
        except (OSError, ValueError):
            reusable = False  # e.g. a continued execution's ref: this store keeps its own copy
        if reusable:
            material["sources"].append({"name": name, "source_ref": prior})
            return prior
        exact, ref, gap = persist_exact_text_source(root, task_id, source_id=name, text=text)
        if gap or exact != text:
            raise OSError(gap.get("reason") or "material source readback mismatch")
        material["sources"].append({"name": name, "source_ref": ref})
        return ref

    if answer_protocol_active(ctx):
        attempted.add("selected-final-answer")  # present or absent, the answer is this execution's
        try:
            text = content if content is not None else str(getattr(getattr(ctx, "_delivery_candidate", None), "full_text", ""))
            answer = selected_final_answer(text, trace)
            if answer:
                ref = retain("selected-final-answer", answer)
                material["selected_answer"] = {"text": answer, "source_ref": ref}
                material["produced"] = True
        except Exception as exc:
            material["issues"].append({"status": "source_unavailable", "reason": str(exc), "tool": "selected_answer"})
    if not include_outputs:
        return material  # Auto without nomination / Off still retain a selected answer.
    try:
        protected = _accept_protected_set(ctx)
        base = task_artifact_dir_path(root, task_id)
        for row in collect_task_artifact_records(root, task_id, measure=False, strict=True, require_registered=True):
            name = str(row.get("relpath") or row["name"])
            if not registered_task_artifact(root, task_id, name) or not row.get("size"):
                continue
            if any(name == str(p).lstrip("/") or name.startswith(str(p).strip("/") + "/")
                   or Path(name).name == Path(str(p)).name for p in protected):
                continue  # hidden/restricted evidence is never opened for admission
            attempted.add(name)
            source = Path(row["path"])
            identity = stream_artifact_file(source, expected=row)
            rel = "source_handles/context_checkpoints/produced-" + identity["sha256"] + ".bin"
            if not (base / rel).exists():
                copy_artifact_file(source, base / rel, expected=identity)
            ref = {"kind": "task_source", "root": "artifact_store", "path": rel, **identity}
            stream_artifact_file(base / rel, expected=identity)
            material["sources"].append({"name": name, "source_ref": ref})
            material["produced"] = True
    except Exception as exc:
        unknown.add("artifact")
        material["issues"].append({"status": "source_unavailable", "reason": str(exc), "tool": "artifact_manifest"})

    # The extension producer owns execution semantics (PluginAPI.register_tool).
    # Read its exact receipt, never infer effects from a method/backend name.
    for call in trace.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        surface = parse_extension_surface_name(str(call.get("tool") or ""))
        if not surface or call.get("is_error"):
            continue
        try:
            result, complete, _gap = materialize_tool_result_source(root, task_id, call)
            if not complete:
                material["issues"].append({"status": "source_unavailable", "reason": str(_gap),
                                           "tool": str(call.get("tool") or "")})
                continue
            try:
                payload = json.loads(result) if isinstance(result, str) else {}
            except ValueError:
                continue  # Plain extension text carries no declared execution fact.
            if not isinstance(payload, dict) or payload.get("observed_effect") is not True:
                continue
            ref = retain("observed-effect-" + str(call.get("tool_call_id") or len(material["sources"])), result)
            material.setdefault("observed_effects", []).append({"tool": call["tool"], "source_ref": ref})
            material["produced"] = True  # producer execution fact, never goal success
        except Exception as exc:
            material["issues"].append({"status": "source_unavailable", "reason": str(exc), "tool": str(call.get("tool") or "")})

    # Only a real task baseline establishes shell-produced changes. An arbitrary
    # dirty working tree is evidence for a nominated review, not admission proof.
    try:
        from ouroboros.mutation_attribution import attributed_git_candidates, _git_path_fingerprints

        getter = getattr(ctx, "active_repo_dir", None)
        repo = getter() if callable(getter) else getattr(ctx, "repo_dir", None)
        if repo:
            accounting = getattr(ctx, "budget_drive_root", None) or root
            snapshot = attributed_git_candidates(accounting, task_id, repo)
            if snapshot.get("candidates"):
                attempted.update(_WORKSPACE_SOURCES)
            if snapshot.get("candidates") and not snapshot.get("blockers"):
                fingerprints = _git_path_fingerprints(Path(repo), snapshot["candidates"])
                diff, meta = _accept_repo_diff(ctx, include_recent_commit=False, drive_root=root, task_id=task_id)
                if meta.get("issue") or fingerprints != _git_path_fingerprints(Path(repo), snapshot["candidates"]):
                    raise ValueError("workspace material changed or capture unavailable")
                ref = retain("produced-workspace", json.dumps({**snapshot, "fingerprints": fingerprints}, sort_keys=True))
                diff_ref = retain("produced-workspace-diff", diff)
                material["workspace"] = {"source_ref": ref, "repo_diff": diff, "repo_diff_source_ref": diff_ref,
                                         "repo_diff_capture": meta.get("capture_disclosure") or {}}
                material["produced"] = True
    except Exception as exc:
        unknown.add("workspace")
        material["issues"].append({"status": "source_unavailable", "reason": str(exc), "tool": "repo_diff"})
    # Another execution's record is the work this new-id retry continues; a later pass of the
    # retry finds that execution in its own record's ``continued_from``.
    owner = str(previous.get("task_id") or "")
    continued = owner if owner != task_id else str((previous.get("continued_from") or {}).get("task_id") or "")
    if continued and continued != task_id:
        _carry_continued_sources(root, task_id, continued, previous, material, attempted, unknown)
    return material


def _carry_continued_sources(root: Any, task_id: str, continued: str, previous: dict, material: dict,
                             attempted: set, unknown: set) -> None:
    """A new-id retry keeps the produced sources of the execution it continues.

    ``previous`` is that execution's recorded material (restored with its working
    checkpoint), or this retry's own on a later pass, whose ``continued_from`` names
    what came from it. Each
    such source this retry does not produce itself is copied into its own store under
    the same reference and keeps its place, so an unchanged delivery keeps the paid
    subject: nothing is re-paid and the old verdict still approves nothing. Material
    the retry produces, re-derives differently or fails to capture changes the subject
    and buys its own review; an unreadable source stays a gap, never a claimed identity.
    """
    marker = previous.get("continued_from") or {}
    rows = [row for row in previous.get("sources") or [] if isinstance(row, dict)]
    names = (set(marker.get("names") or ()) if marker.get("task_id") == continued
             else {str(row.get("name") or "") for row in rows})
    derived = {row["name"]: row for row in material["sources"]}
    merged = []
    for row in rows:
        name, ref = str(row.get("name") or ""), row.get("source_ref") if isinstance(row.get("source_ref"), dict) else {}
        if name in derived:
            merged.append(derived.pop(name))
            continue
        kind = ("artifact" if str(ref.get("path") or "").startswith(_PRODUCED_FILES)
                else "workspace" if name in _WORKSPACE_SOURCES else "")
        if name not in names or name in attempted or kind in unknown:
            continue  # this retry's own material is never resurrected
        try:
            _place_continued_source(root, task_id, continued, ref)
        except Exception as exc:
            material["issues"].append({"status": "source_unavailable", "reason": str(exc), "tool": "continued_material"})
            continue
        merged.append({"name": name, "source_ref": ref})
        material["produced"] = True
    material["sources"] = merged + list(derived.values())
    if names:
        material["continued_from"] = {"task_id": continued, "names": sorted(names)}


def _place_continued_source(root: Any, task_id: str, continued: str, ref: dict) -> None:
    """Copy one source of ``continued`` to the same path in this task's store, verified."""
    from pathlib import PurePosixPath

    from ouroboros.artifacts import (
        copy_artifact_file, read_actor_source_bytes, stream_artifact_file, task_artifact_dir_path,
    )
    from ouroboros.task_custody import fence_publication
    from ouroboros.utils import write_bytes_atomic

    rel = PurePosixPath(str(ref.get("path") or ""))
    identity = {"size": ref.get("size"), "sha256": ref.get("sha256")}
    if ((ref.get("kind"), ref.get("root")) != ("task_source", "artifact_store") or rel.is_absolute()
            or rel.parts[:1] != ("source_handles",) or ".." in rel.parts
            or not isinstance(identity["size"], int) or not identity["sha256"]):
        raise ValueError("continued material is not a verified task source")

    def inside(owner: str, *, create: bool = False) -> Path:
        base = task_artifact_dir_path(root, owner, create=create).resolve(strict=False)
        path = base.joinpath(*rel.parts)
        if path.is_symlink() or not path.resolve(strict=False).is_relative_to(base):
            raise ValueError("continued material escapes its task store")
        return path

    target = inside(task_id, create=True)
    try:
        stream_artifact_file(target, expected=identity)
        return  # placed by an earlier pass of this retry
    except OSError:
        pass
    source = inside(continued)
    if source.is_file():
        copy_artifact_file(source, target, expected=identity)  # streamed and verified
    else:  # packed or retained bytes of the continued execution
        fence_publication()
        target.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(target, read_actor_source_bytes(root, continued, ref))
    stream_artifact_file(target, expected=identity)
