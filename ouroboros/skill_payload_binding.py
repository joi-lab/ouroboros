"""Effective skill-payload location selection for Tool API bindings.

Physical package placement stays authoritative for confinement.  This leaf only
projects the existing ownership facts that tool discovery already exposes:
launcher-seeded native payloads retain ``native`` authority, while an existing
markerless native payload is a user-managed logical ``external`` payload.
"""

from __future__ import annotations

import pathlib
from typing import Optional

from ouroboros.skill_loader import (
    _classify_skill_source,
    _sanitize_skill_name,
    _select_skill_location,
    _skill_location_inventory,
    _SkillLocationCandidate,
)

_MANIFEST_NAMES = ("SKILL.md", "skill.json")

# Parent-equivalent payload reads do not change native mutation or selected-skill authority.
_NATIVE_PAYLOAD_READ_OPERATIONS = frozenset({"read", "list", "search"})
_NATIVE_PAYLOAD_READ_PROFILES = frozenset({
    "local_readonly_subagent",
    "acting_subagent",
    "operator_control",
})


def _native_payload_read_allowed(
    *,
    profile: str,
    operation: str,
    requested: str,
) -> bool:
    """Admit explicit read selectors without granting a mutation operation."""
    return (
        requested in {"native", "user_repo"}
        and operation in _NATIVE_PAYLOAD_READ_OPERATIONS
        and profile in _NATIVE_PAYLOAD_READ_PROFILES
    )


def selected_skill_publish_name(ctx: object) -> str:
    """Return the host-bound publication target from the active task context."""
    if str(getattr(ctx, "current_task_type", "") or "") != "skill_publish":
        return ""
    metadata = getattr(ctx, "task_metadata", {})
    target = metadata.get("skill_publish_target", {}) if isinstance(metadata, dict) else {}
    raw = str(target.get("skill") or "") if isinstance(target, dict) else ""
    canonical = _sanitize_skill_name(raw)
    return canonical if raw.strip() and canonical != "_unnamed" else ""


def _is_manifestless_user_repo(candidate: _SkillLocationCandidate) -> bool:
    return candidate.location == "user_repo" and not any(
        (candidate.skill_dir / name).is_file() for name in _MANIFEST_NAMES
    )


def selected_manifestless_user_repo_name(
    ctx: object,
    drive_root: pathlib.Path,
) -> str:
    """Return the exact manifestless user-repo leaf advertised to the model."""
    name = selected_skill_publish_name(ctx)
    if not name:
        return ""
    identity = tuple(
        candidate
        for candidate in _skill_location_inventory(
            drive_root,
            selected_manifestless_name=name,
        )
        if candidate.name == name
    )
    if len(identity) == 1 and _is_manifestless_user_repo(identity[0]):
        return name
    return ""


def selected_manifestless_publish_name(
    ctx: object,
    *,
    name: str,
    operation: str,
    allow_manifest_write: bool,
) -> str:
    """Admit only the selected task target to manifestless discovery."""
    if selected_skill_publish_name(ctx) == name and (operation in {"read", "list", "search"} or allow_manifest_write):
        return name
    return ""


def select_effective_skill_location(
    drive_root: pathlib.Path,
    *,
    name: str,
    location: str,
    require_unique_identity: bool,
    selected_manifestless_name: str = "",
) -> tuple[Optional[_SkillLocationCandidate], str]:
    """Return one physical package plus its model-visible effective source."""
    candidates = _skill_location_inventory(
        drive_root,
        selected_manifestless_name=selected_manifestless_name,
    )
    canonical_name = _sanitize_skill_name(name)
    identity = tuple(item for item in candidates if item.name == canonical_name)
    requested = location or (identity[0].location if identity else "")
    if not requested:
        return None, ""

    native_aliases = tuple(
        item
        for item in identity
        if item.location == "native"
        and _classify_skill_source(
            item.skill_dir,
            location="native",
            drive_root=drive_root,
        )
        != "native"
    )
    selected: Optional[_SkillLocationCandidate]
    if requested == "external" and native_aliases:
        ordinary_external = tuple(item for item in identity if item.location == "external")
        if ordinary_external or (require_unique_identity and len(identity) > 1):
            evidence = ", ".join(f"{item.location}:{item.skill_dir}" for item in identity)
            raise ValueError(f"Skill name collision for {canonical_name!r}: {evidence}")
        selected = native_aliases[0]
    else:
        selected = _select_skill_location(
            candidates,
            name=canonical_name,
            location=requested,
            require_unique_identity=require_unique_identity,
        )
    if selected is None:
        return None, ""

    source = selected.location
    if source == "native":
        source = (
            "native"
            if _classify_skill_source(
                selected.skill_dir,
                location="native",
                drive_root=drive_root,
            )
            == "native"
            else "external"
        )
    return selected, source


def resolve_skill_payload_base(
    ctx: object,
    *,
    drive_root: pathlib.Path,
    profile: str,
    top_level: bool,
    operation: str,
    location: str,
    skill_name: str,
    allow_missing: bool = False,
) -> tuple[pathlib.Path, str, str]:
    """Resolve one Tool API selector to its physical payload root."""
    from ouroboros.contracts.task_constraint import normalize_task_constraint
    from ouroboros.contracts.skill_payload_policy import resolve_constrained_payload_path

    constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    if constraint and constraint.has_selected_skill:
        # The task's selected payload supplies omitted selectors, not a new target.
        parts = constraint.payload_root.split("/")
        skill_name = str(skill_name or "").strip() or constraint.skill_name
        location = str(location or "").strip() or (parts[1] if len(parts) == 3 else "")
    requested = str(location or "").strip().lower()
    canonical_name = _sanitize_skill_name(skill_name)
    if not str(skill_name or "").strip() or canonical_name == "_unnamed":
        raise ValueError("root=skill_payload requires a non-empty skill_name")
    selected_manifestless = selected_manifestless_publish_name(
        ctx,
        name=canonical_name,
        operation=operation,
        allow_manifest_write=allow_missing,
    )
    allowed = {"external", "clawhub", "ouroboroshub", "native", "user_repo"}
    if requested not in allowed and not (not requested and (operation == "review" or selected_manifestless)):
        raise ValueError(
            "root=skill_payload requires bucket/location in external|clawhub|ouroboroshub|native|user_repo"
        )
    native_read_allowed = _native_payload_read_allowed(
        profile=profile,
        operation=operation,
        requested=requested,
    )
    if (
        requested in {"native", "user_repo"}
        and not top_level
        and not native_read_allowed
    ):
        raise ValueError(f"profile={profile} cannot select skill location={requested}")
    selected, source = select_effective_skill_location(
        drive_root,
        name=canonical_name,
        location=requested,
        require_unique_identity=(operation not in {"read", "list", "search"} or bool(selected_manifestless)),
        selected_manifestless_name=selected_manifestless,
    )
    if selected is not None:
        if constraint and constraint.has_selected_skill:
            expected = resolve_constrained_payload_path(drive_root, constraint, ".")
            if selected.skill_dir.resolve(strict=False) != expected:
                raise ValueError("SKILL_REDIRECT_BLOCKED: this task selected a different skill payload")
        if (
            selected.location in {"native", "user_repo"}
            and not top_level
            and not native_read_allowed
        ):
            raise ValueError(f"profile={profile} cannot select skill location={selected.location}")
        if (
            not requested
            and operation != "review"
            and selected_manifestless
            and not _is_manifestless_user_repo(selected)
        ):
            raise ValueError("bucket may be omitted only for the exact selected manifestless user_repo target")
        if source == "native" and operation in {"write", "edit", "shell"}:
            raise ValueError("installed native skills are read/review only; edit their seed via root=system_repo")
        return selected.skill_dir.resolve(strict=False), source, selected.name
    if constraint and constraint.has_selected_skill:
        raise ValueError("SKILL_REDIRECT_BLOCKED: the selected installed skill payload was not found")
    if not requested and operation == "review":
        raise ValueError(f"skill {canonical_name!r} was not found")
    if requested == "native" and operation in {"write", "edit", "shell"}:
        raise ValueError("installed native skills are read/review only; edit their seed via root=system_repo")
    if (
        operation == "write"
        and allow_missing
        and requested
        in {
            "external",
            "clawhub",
            "ouroboroshub",
        }
    ):
        return (
            (drive_root / "skills" / requested / canonical_name).resolve(strict=False),
            requested,
            canonical_name,
        )
    raise ValueError(f"skill {canonical_name!r} was not found in location {requested!r}")


__all__ = [
    "resolve_skill_payload_base",
    "select_effective_skill_location",
    "selected_manifestless_publish_name",
    "selected_manifestless_user_repo_name",
    "selected_skill_publish_name",
]
