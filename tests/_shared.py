"""Shared helpers for the Ouroboros test suite.

These functions are reused across multiple ``tests/test_*.py`` modules to
avoid duplicated boilerplate (extension-loader cleanup, mock contexts).
They are intentionally plain module-level callables, not fixtures — many
callers need them at module import time.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
from contextlib import contextmanager
from unittest.mock import MagicMock


def producer_evidence_dir(fallback, name, *, env=()):
    """Where one test producer's own diagnostics go: one destination per invocation.

    A destination the operator exported explicitly for this family wins unchanged. Otherwise
    the shared channel decides: ``diagnostic_directory`` redirects to the launcher's
    ``OUROBOROS_TEST_EVIDENCE_ROOT`` (scripts/safe_test.py and the native preflight both
    publish it) and falls back to ``fallback`` for a bare pytest, where nothing vouches for a
    directory outside the disposable scratch. A variable private to one test used to fall
    back straight into ``tmp_path``, so a released scratch took its screenshots, DOM and JSON
    with it while the migrated destinations survived.
    """
    from ouroboros.test_evidence import diagnostic_directory

    for key in ((env,) if isinstance(env, str) else tuple(env)):
        explicit = os.environ.get(key, "").strip()
        if explicit:
            path = pathlib.Path(explicit)
            path.mkdir(parents=True, exist_ok=True)
            return path
    return diagnostic_directory(fallback, name)


def retire_synthetic_retention(directory, *reasons) -> bool:
    """Remove the retention marker THIS test manufactured, only when it says exactly ``reasons``.

    A fixture that fakes an unproven hold or an export fault proves no surviving
    process, yet its marker would keep the whole enclosing run. Any other line may
    describe a real one, so it keeps the marker. True when no marker remains.
    """
    from ouroboros.test_environment import RETENTION_MARKER

    marker = pathlib.Path(directory) / RETENTION_MARKER
    if marker.is_symlink():
        return False
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return True
    except (OSError, UnicodeError):
        return False
    known = {line for reason in reasons for line in reason.rstrip().splitlines()}
    if not lines or not known.issuperset(lines):
        return False
    marker.unlink()
    return True


def retention_left_in(directory) -> str:
    """Name the retention markers beneath ``directory`` with each one's first reason; "" when none.

    The conftest retention guard's report over release_scratch's own walk: an unreadable
    subtree, which that walk also counts, is not a marker a test left.
    """
    from ouroboros.test_environment import RETENTION_MARKER, retention_markers

    left = []
    for marker in retention_markers(directory):
        if marker.name != RETENTION_MARKER:
            continue
        try:
            reason = marker.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, UnicodeError, IndexError):
            reason = "unreadable"
        left.append(f"{marker.parent} ({reason})")
    return "; ".join(left[:3]) + (f" (+{len(left) - 3} more)" if len(left) > 3 else "")


def reap_test_process_group(proc, *, timeout_sec: float = 5.0) -> None:
    """Reap a test child started in its own session; prove its helpers are gone.

    A best-effort signal can be refused for an already-exited group. Only the
    existing fresh member census decides cleanup success; unknown stays live.
    """
    import time
    from ouroboros.platform_layer import kill_process_group_id
    from ouroboros.process_containment import process_group_has_live_members

    kill_process_group_id(proc.pid)
    proc.wait(timeout=timeout_sec)
    deadline = time.monotonic() + timeout_sec
    while process_group_has_live_members(proc.pid):
        assert time.monotonic() < deadline, f"test process group {proc.pid} still has live or unknown members"
        time.sleep(0.05)


def stop_socket_sharer() -> None:
    """Stop multiprocessing's socket-transfer server once a test's spawned children are gone.

    On Windows a spawn child receives a socket argument (the worker stop channel) through the
    stdlib ``resource_sharer``, whose process-global ``_serve`` thread starts with the first such
    spawn and never ends by itself: it belongs to the test that spawned the child. Elsewhere
    nothing started it and this is a no-op; a later spawn starts a fresh one."""
    from multiprocessing import resource_sharer

    resource_sharer.stop(timeout=5)


def wait_test_child_stop(pid: int, *, timeout_sec: float = 3.0) -> dict:
    """Retain a fixture child's terminal observation before diagnostic reads.

    A zombie can be reaped between two probes. Check that state first, and keep
    the observed stop instead of asking again after the wait already succeeded.
    A still-live or unreadable child exhausts the original bound, never passes.
    """
    import time
    from ouroboros.platform_layer import pid_is_alive
    from ouroboros.process_containment import pid_is_zombie

    deadline = time.monotonic() + timeout_sec
    while True:
        stopped = pid_is_zombie(pid) or not pid_is_alive(pid)
        verdict = {"child_stopped": stopped, "t_ns": time.monotonic_ns()}
        if stopped or time.monotonic() >= deadline:
            return verdict
        time.sleep(.01)


# Every function in the tree that persists a settings document, as (repo-relative POSIX
# path, function name). Three route through `config.prepare_settings_for_persist`; the
# context-pair migration and the Colab generator are exempt from it by design and carry
# their reason in the prologue tripwire's exempt table. Two pins read this one list: the
# tripwire in tests/test_runtime_mode_elevation.py proves the scanned roots hold exactly
# these writers (plus its declared non-writer matches), and the byte pin in
# tests/test_settings_read_seam.py drives every one of them through its real entry point.
SETTINGS_WRITERS = (
    ("ouroboros/config.py", "save_settings"),
    # The owner endpoints' write lives in the locked read-modify-write primitive.
    ("ouroboros/gateway/owner_settings.py", "_owner_update_settings"),
    ("ouroboros/packaged_cli.py", "_save_settings"),
    ("ouroboros/context_mode_compat.py", "normalize_and_persist_context_mode_compat"),
    ("ouroboros/colab_bootstrap.py", "write_colab_settings"),
)

_SETTINGS_WRITE_SHAPES = re.compile(
    r"\.write_text\(|\.write_bytes\(|\.write\(|json\.dump\("
    r"|atomic_write_json\(|write_text_atomic\(|write_bytes_atomic\("
    # A rename or copy that lands on the settings path is a commit too (the
    # packaged saver once committed through replace_atomic); bare `.replace(` /
    # `.rename(` are NOT here — str.replace matches unrelated functions.
    r"|os\.replace\(|replace_atomic\(|shutil\.(?:copy|copyfile|copy2|move)\("
)


def calls_function(node: ast.AST, name: str) -> bool:
    """Whether ``node`` contains a call to ``name`` — as a bare name or as an attribute."""
    return any(
        isinstance(call, ast.Call)
        and (getattr(call.func, "id", "") == name or getattr(call.func, "attr", "") == name)
        for call in ast.walk(node)
    )


def settings_writers(repo: pathlib.Path) -> dict[tuple[str, str], bool]:
    """Every function under ``ouroboros/**``, ``supervisor/**``, ``server.py`` and
    ``launcher.py`` that persists a settings document, mapped to whether it CALLS the
    persistence prologue. The one predicate both settings-writer pins read.

    A function is a writer when it calls the one serializer (that is a settings document
    by definition), or when it names the settings path or file — ``SETTINGS_PATH``, a
    ``settings_path`` parameter, the ``settings.json`` literal — or carries "settings" in
    its own name, AND does a write-shaped thing: a ``.write_text`` / ``.write_bytes`` /
    ``.write`` / ``json.dump`` call, one of the atomic helpers, or a rename/copy commit
    (``os.replace``, ``replace_atomic``, ``shutil.copy*``/``move``). The shape half is a
    finite list: a writer that names the path but commits through a shape outside it is
    invisible to the scan. Both halves read the
    function's whole source, so prose can only ADD a candidate. Routing is read from the
    CALLS the function makes, never from its text: a docstring that merely named the
    prologue once vouched for a writer that never called it, which is the fail-open
    direction. Every file is parsed — a file-level prefilter once hid a writer whose module
    carried none of the prefilter's tokens. Keys are POSIX so lookups hold on Windows."""
    writers: dict[tuple[str, str], bool] = {}
    files = (sorted((repo / "ouroboros").rglob("*.py")) + [repo / "server.py", repo / "launcher.py"]
             + sorted((repo / "supervisor").rglob("*.py")))
    for path in files:
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines(keepends=True)
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seg = "".join(lines[node.lineno - 1:node.end_lineno])
            names_settings = ("settings_path" in seg.lower() or "settings.json" in seg
                              or "settings" in node.name.lower())
            if calls_function(node, "serialize_settings") or (
                    names_settings and _SETTINGS_WRITE_SHAPES.search(seg)):
                writers[(path.relative_to(repo).as_posix(), node.name)] = calls_function(
                    node, "prepare_settings_for_persist")
    return writers


def clean_extension_runtime_state() -> None:
    """Reset every extension_loader namespace to a pristine state.

    Superset of cleanup logic that previously lived (with minor variations)
    in ``test_skill_exec.py``, ``test_extensions_api.py`` and
    ``test_extension_loader.py``. Extra clears are inert when the namespace
    is already empty, so the superset is safe for every caller.
    """
    from ouroboros import extension_loader
    from ouroboros.extension_reconcile_queue import _adopted_generations

    _adopted_generations.clear()
    with extension_loader._lock:
        loaded_names = list(extension_loader._extensions.keys())
    for name in loaded_names:
        extension_loader.unload_extension(name)
    with extension_loader._lock:
        extension_loader._extension_modules.clear()
        extension_loader._load_failures.clear()
        extension_loader._unloading.clear()
        extension_loader._lifecycle_locks.clear()
        extension_loader._tools.clear()
        extension_loader._routes.clear()
        extension_loader._ws_handlers.clear()
        extension_loader._ui_tabs.clear()
        extension_loader._settings_sections.clear()
        extension_loader.set_ws_broadcaster(None)


def make_safe_mock_ctx(tmp_path, *, repo_dir=None):
    """Return a MagicMock ToolContext whose drive paths resolve to real dirs.

    Several observability paths append to ``ctx.drive_logs() / "events.jsonl"``.
    A bare MagicMock would stringify into a filename in the repo root.
    """
    ctx = MagicMock()
    ctx.repo_dir = repo_dir if repo_dir is not None else tmp_path
    ctx.drive_root = tmp_path
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    ctx.drive_logs.return_value = logs
    ctx.emit_progress_fn = lambda *a, **kw: None
    ctx.task_id = "test-task"
    return ctx


def configure_test_subagent(
    monkeypatch,
    *,
    subagent_id: str = "api-scout",
    kind: str = "api_model",
    target: str = "openai/gpt-5.6-sol",
    profile_id: str = "",
    effort: str = "high",
) -> str:
    """Install one explicit Available-subagent row for scheduling tests."""
    route = {"kind": kind, "target_id": target}
    if kind == "agent_session" and profile_id:
        route["credential_profile_id"] = profile_id
    monkeypatch.setenv("OUROBOROS_SUBAGENTS", json.dumps({
        "enabled": True,
        "items": [{
            "subagent_id": subagent_id,
            "name": "Test subagent",
            "recommended_use": "Production-shaped test actor.",
            "route": route,
            "effort": effort,
        }],
    }))
    return subagent_id


def reconcile_receipt(action=None, reason=None):
    """Shape of ``_reconcile_extension_payload``'s return value for a test fake.

    The real helper returns a receipt dict that also names the answering process
    and the worker-to-server marker outcome, so a fake returning the older
    2-tuple silently breaks the caller. Keep fakes going through here.
    """
    return {"action": action, "reason": reason, "process": "", "server_reconcile": ""}


def live_worker_pids(data_root):
    """The ledger's live worker identities, including across pool respawns."""
    from ouroboros.process_custody import _read_ledger_strict, _fingerprint_matches
    readable, entries = _read_ledger_strict(data_root)
    assert readable, "worker custody is unreadable"
    return {int(row["pid"]) for row in entries
            if str(row.get("purpose") or "").startswith("worker:") and _fingerprint_matches(row)}


def write_queue_document(path, payload):
    """Publish a fixture document and its matching liveness stamp; retain its date."""
    from ouroboros.utils import atomic_write_json
    payload = {**payload, "generation": "fixture-generation"}
    atomic_write_json(path, payload)
    atomic_write_json(path.parent / "queue.alive", {
        "generation": payload["generation"], "ts": payload.get("ts"), "heartbeats": {},
    })
    return payload


# Measuring a reviewer's window (`reviewer_context_window` / `scope_window` ->
# `capability_evidence.probe`) asks OpenRouter's live `/models` catalog through
# `LLMClient.openrouter_context_length(allow_fetch=True)`; a retrieving work
# order, the `## Review` block's cost hint and the retrieving brief all measure.
# The answer lands in these CLASS caches for the rest of the process, so a later
# unrelated send already strips a field the catalog lists as unsupported (the
# `test_llm_no_proxy` parameter-rejection retry then has nothing left to drop).
_PROVIDER_CATALOG_CLASS_STATE = (
    "_SUPPORTED_PARAMS_CACHE", "_SUPPORTED_PARAMS_FETCHED",
    "_CONTEXT_LENGTH_CACHE", "_CAPABILITIES_FETCH_OK",
)
_PROVIDER_CATALOG_PRISTINE = {
    "_SUPPORTED_PARAMS_CACHE": {}, "_SUPPORTED_PARAMS_FETCHED": False,
    "_CONTEXT_LENGTH_CACHE": {}, "_CAPABILITIES_FETCH_OK": False,
}


def _provider_catalog_snapshot(cls):
    import copy
    return {name: copy.copy(getattr(cls, name)) for name in _PROVIDER_CATALOG_CLASS_STATE}


def _restore_provider_catalog(cls, saved):
    for name, value in saved.items():
        current = getattr(cls, name)
        if isinstance(current, dict) and isinstance(value, dict):
            current.clear()
            current.update(value)  # the mixin's dict object itself, not a rebinding
        else:
            setattr(cls, name, value)


@contextmanager
def offline_provider_catalog(monkeypatch):
    """Body of conftest's ``provider_catalog_offline`` fixture, for a process that
    has no fixture injection. A spawned child must not import ``tests.conftest``:
    its import-time isolation would rebind the inherited HOME, data, settings,
    TMPDIR and pycache prefix after config modules already read them.
    """
    from ouroboros.llm import LLMClient

    saved = _provider_catalog_snapshot(LLMClient)
    monkeypatch.setattr(LLMClient, "_fetch_openrouter_capabilities", classmethod(lambda cls: None))
    try:
        yield saved
    finally:
        _restore_provider_catalog(LLMClient, saved)
