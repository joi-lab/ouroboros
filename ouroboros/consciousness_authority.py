"""What a consciousness wake-up may do, and how that authority follows its work.

Three autonomy levels (owner decision В10', PLAN 5.4): ``observe`` researches,
keeps internal notes and may manage existing schedules while remaining
resource-narrowed, ``act`` (the default) starts and steers work but may not evolve,
restart, change settings or touch its own alarm, ``full`` is an owner turn in
everything the install's runtime mode allows. A wake carries the level it
started under as ``metadata.consciousness_autonomy`` beside its origin label
``metadata.initiator = "consciousness"``; both are inherited by everything the
wake starts (a promoted root, a follow-up, a subagent, an evolution cycle)
through ``consciousness_origin_metadata``, so a second generation never
falls out of the level or the allowance.

One helper derives the level's two structural consequences at task build
(``apply_consciousness_authority``): the contract's ``disabled_tools`` (an
exception list, never an allowlist — a new READ tool is available to Observe
by default) and ``runtime_mode_cap`` (Act/Observe run under ``light`` even on
an advanced/pro/cyber_pro install: owner decision В21=A, "may write, but not
into its own repository"). The registry reads both from the task metadata;
for a consciousness-origin task the disabled list is enforced at DISPATCH ONLY
(В31=B) so the schema prefix stays byte-identical to an owner turn's and the
provider prompt cache is shared (``registry_guards._disabled_tools``).
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from ouroboros.runtime_limits import CONSCIOUSNESS_AUTONOMY_LEVELS, get_consciousness_autonomy
from ouroboros.settings_scales import VALID_RUNTIME_MODES
from ouroboros.tool_capabilities import FOREGROUND_MUTATIVE_TOOLS, OBSERVE_WORLD_MUTATION_TOOLS

CONSCIOUSNESS_INITIATOR = "consciousness"
# Ledger category of a wake's own rows vs the rows of the work it started.
CONSCIOUSNESS_CATEGORY = "consciousness"
CONSCIOUSNESS_TASK_CATEGORY = "consciousness_task"
LEVELS = CONSCIOUSNESS_AUTONOMY_LEVELS
LEVEL_OBSERVE, LEVEL_ACT, LEVEL_FULL = LEVELS

# Act withholds the runtime's own posture: evolution, restart, the one
# settings writer, its own alarm clock and owner-binding configuration.
ACT_DISABLED: tuple[str, ...] = (
    "toggle_evolution", "request_restart", "set_tool_timeout",
    "toggle_consciousness", "configure_presence",
)
# Names Observe keeps even though they appear in the mutation table above,
# because each has a real read-only or own-work path that withholding the NAME
# would destroy along with the mutation. What the name may then be asked to DO
# is narrowed by ``observe_argument_refusal`` at dispatch, on the arguments.
OBSERVE_ARGUMENT_NARROWED: frozenset[str] = frozenset({
    # Research children and delegated research sessions: read-only shapes only.
    "schedule_subagent", "delegate_start",
    # Its own task artifacts and project workpad/journal — not owner files,
    # not source, not the runtime's own controls.
    "write_file", "edit_text", "journal_write", "workpad_write",
    # Its own children: starting one is narrowed above, and a mind that may start
    # a child must be able to stop the one it started (the nanny's other half).
    "cancel_task",
})
# Observe withholds every verb that starts work or changes the world beyond
# this mind's own memory (the table beside ``ROUTING_VERBS``) plus publication.
OBSERVE_DISABLED: tuple[str, ...] = ACT_DISABLED + tuple(sorted(
    (OBSERVE_WORLD_MUTATION_TOOLS | FOREGROUND_MUTATIVE_TOOLS)
    - set(ACT_DISABLED)
    - OBSERVE_ARGUMENT_NARROWED
))
# The nanny verb of a running campaign is never withheld (owner decision В12 A).
NEVER_DISABLED: frozenset[str] = frozenset({"steer_task"})
# The only file roots Observe may write: its own task drive and artifact store.
# Both are already confined by the file tools; this is what it is ALLOWED to
# choose, not a new path authority.
OBSERVE_WRITE_ROOTS: frozenset[str] = frozenset({"task_drive", "artifact_store"})


def normalize_level(value: Any) -> str:
    """The closed level enum; an unknown value falls back to the owner's setting."""
    text = str(value or "").strip().lower()
    return text if text in LEVELS else get_consciousness_autonomy()


def disabled_tools_for(level: Any) -> list[str]:
    """The contract's ``disabled_tools`` for a level (Full: none)."""
    normalized = normalize_level(level)
    if normalized == LEVEL_FULL:
        return []
    names = OBSERVE_DISABLED if normalized == LEVEL_OBSERVE else ACT_DISABLED
    return [name for name in names if name not in NEVER_DISABLED]


def runtime_mode_cap_for(level: Any) -> str:
    """``light`` for Act/Observe (the per-task mode cap), ``""`` = no cap for Full."""
    return "" if normalize_level(level) == LEVEL_FULL else "light"


def is_consciousness_origin(metadata: Any) -> bool:
    """Whether a task (metadata mapping) was started by consciousness or its tree."""
    source = metadata if isinstance(metadata, Mapping) else {}
    return str(source.get("initiator") or "").strip() == CONSCIOUSNESS_INITIATOR


def consciousness_origin_metadata(parent_metadata: Any) -> Dict[str, Any]:
    """The origin keys a task started from a consciousness turn/tree inherits.

    Empty for any other parent. The child is a *started* root, so its ledger
    category is ``consciousness_task``; the level rides along so the child's
    own build derives the same ``disabled_tools``/``runtime_mode_cap``.
    """
    if not is_consciousness_origin(parent_metadata):
        return {}
    return {
        "initiator": CONSCIOUSNESS_INITIATOR,
        "usage_category": CONSCIOUSNESS_TASK_CATEGORY,
        "consciousness_autonomy": normalize_level(parent_metadata.get("consciousness_autonomy")),
    }


def apply_consciousness_authority(task: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the level's two structural consequences onto a consciousness-origin task's metadata.

    Called BEFORE the contract is attached (``attach_task_contract`` reads
    ``metadata.disabled_tools`` into the contract when the contract has none).
    ``disabled_tools`` is only derived when the producer set none, so an
    explicit list (a test, a narrower parent) stands; the mode cap is always
    the level's. Any other task is returned untouched.
    """
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else None
    if metadata is None or not is_consciousness_origin(metadata):
        return task
    level = normalize_level(metadata.get("consciousness_autonomy"))
    metadata["consciousness_autonomy"] = level
    if metadata.get("disabled_tools") is None:
        metadata["disabled_tools"] = disabled_tools_for(level)
    metadata["runtime_mode_cap"] = runtime_mode_cap_for(level)
    task["metadata"] = metadata
    return task


def task_disabled_tools(task: Mapping[str, Any]) -> frozenset[str]:
    """Every name a task record withholds, from all three carriers the registry unions."""
    metadata = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
    contract = task.get("task_contract") if isinstance(task.get("task_contract"), Mapping) else {}
    if not contract and isinstance(metadata.get("task_contract"), Mapping):
        contract = metadata["task_contract"]
    names: set[str] = set()
    for source in (task, metadata, contract):
        raw = source.get("disabled_tools")
        if isinstance(raw, (list, tuple)):
            names.update(str(name).strip() for name in raw if str(name).strip())
    return frozenset(names)


def task_mode_capped_light(task_metadata: Any) -> bool:
    """Whether a task carries the light per-task cap (an Act/Observe consciousness tree). The
    cap is the LEVEL's, not the install's: such a tree may write, but never into its own
    repository — a self_worktree child or a system-repo patch integration is refused in every
    install mode, the mutative-subagent toggle included (В21=A, PLAN §5.4)."""
    metadata = task_metadata if isinstance(task_metadata, Mapping) else {}
    return str(metadata.get("runtime_mode_cap") or "").strip().lower() == "light"


def effective_runtime_mode(install_mode: str, task_metadata: Any) -> str:
    """The stricter of the install's runtime mode and the task's ``runtime_mode_cap``.

    Rank light < advanced < pro < cyber_pro (``settings_scales``). The three
    light gates of the tool dispatcher read this instead of the bare install
    mode; ``get_runtime_mode()`` itself never changes.
    """
    from ouroboros.runtime_mode_policy import runtime_mode_at_least

    metadata = task_metadata if isinstance(task_metadata, Mapping) else {}
    cap = str(metadata.get("runtime_mode_cap") or "").strip().lower()
    if cap in VALID_RUNTIME_MODES and runtime_mode_at_least(str(install_mode), cap):
        return cap
    return str(install_mode)


def is_observe_origin(task_metadata: Any) -> bool:
    """Whether this task runs under a consciousness wake at the Observe level.

    The one predicate every dispatch-time narrowing asks, so "what Observe is"
    is decided here rather than re-derived, slightly differently, per call site.
    """
    metadata = task_metadata if isinstance(task_metadata, Mapping) else {}
    return (is_consciousness_origin(metadata)
            and normalize_level(metadata.get("consciousness_autonomy")) == LEVEL_OBSERVE)


def observe_argument_refusal(task_metadata: Any, name: str, args: Mapping[str, Any]) -> str:
    """The refusal text for an Observe call whose ARGUMENTS ask for mutation; '' to allow.

    Observe keeps the names in ``OBSERVE_ARGUMENT_NARROWED`` visible because each
    has a genuine read-only or own-work use. This is the seam that stops the
    same name from carrying back the authority the level removed: a write root
    outside its own drive, a mutating child, a writing delegated session. The
    positive path is deliberately left open — a refusal here is about the
    arguments, never about the tool.
    """
    if not is_observe_origin(task_metadata):
        return ""
    tool = str(name or "").strip()
    values = args if isinstance(args, Mapping) else {}
    if tool in {"write_file", "edit_text"}:
        # The file tools already confine a resolved path inside the chosen base;
        # this decides which base Observe may choose at all.
        root = str(values.get("root") or "active_workspace").strip()
        if root not in OBSERVE_WRITE_ROOTS:
            return (
                "⚠️ RESOURCE_CONSTRAINT_BLOCKED: Observe may write its own task_drive or "
                "artifact_store only; use the cognitive-memory or project-note tools for "
                "everything else it wants to keep."
            )
    elif tool == "schedule_subagent":
        surface = str(values.get("write_surface") or "").strip().lower()
        if surface not in ("", "read_only") or bool(values.get("may_mutate")):
            return (
                "⚠️ RESOURCE_CONSTRAINT_BLOCKED: Observe may schedule read-only research "
                "children only; omit write_surface and may_mutate."
            )
    elif tool == "delegate_start":
        # An omitted access is the READ-ONLY shape here (``_derive_authority``
        # pins it), so only an explicit write request is refused.
        if str(values.get("root") or "").strip() or str(values.get("access") or "").strip().lower() == "workspace_write":
            return (
                "⚠️ RESOURCE_CONSTRAINT_BLOCKED: Observe may start a delegated read-only "
                "research session only; omit root and leave access readonly."
            )
    return ""
