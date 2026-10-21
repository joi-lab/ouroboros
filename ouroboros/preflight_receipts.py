"""Unsigned durable receipts of the complete default hermetic test gate.

``commit_admission.PreflightTestProof`` is process-held, so a new process or a
Restart repeated the whole battery for an unchanged subject. Owner decision 3A
(12.10.2026): one full run per fixed code version, accepted after a restart
when everything matches, shown to reviewers, unsigned. This module keeps ONE
small JSON per exact identity under the caller's canonical data root. Only
``preflight_runner.run_hermetic_pytest`` writes it, after a complete default
run (the node lane when the candidate carries web tests, the parallel
``not serial`` pass green on two observed xdist workers or the one admitted, the
flag-free serial pass, a proven containment reap). That runner reads it at its
existing reuse seam after the baseline and assembly checks; adoption reads it for
exactly its commit (``preflight_qualification.receipt_for_commit``).

The identity is the process-held proof (tested HEAD, tree, index and effective
workload), the installation the test interpreter loads (probed under the exact
scrubbed child environment), the Git the tests invoke, and the executing
source of THIS process: a one-time baseline taken where its code generation is
loaded, re-read from disk before reuse and after green. A process whose loaded
code may predate the disk, or that never captured, still runs the tests; it
only cannot accept or mint a receipt.

Unsigned: the file is ordinary local writable state and a permitted writer can
forge it; nothing here authenticates a writer or a malicious local edit. A
receipt records one historical green execution, never "the latest attempt
passed", and a later red run deletes nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import shutil
import subprocess
import threading
from typing import Any, Optional

log = logging.getLogger(__name__)

RECEIPT_SCHEMA = 1
RECEIPTS_DIR = "preflight_receipts"  # <canonical data root>/state/preflight_receipts/<key>.json

# The executing implementation a server, worker or standalone review entry runs its
# gate from: Python beneath these trees of the package root, plus the root entry files.
_SOURCE_TREES = ("ouroboros", "supervisor", "scripts")
_SOURCE_FILES = ("server.py", "launcher.py")

_baseline_lock = threading.Lock()
_baseline: Optional[str] = None  # None: never captured; "": unavailable for this memory image


def canonical_data_root(ctx: Any) -> Optional[pathlib.Path]:
    """The caller's canonical data root, chosen from its own metadata before any test
    isolation (``tool_access_paths``: task budget root, ctx budget root, drive root);
    ``None`` for a ctx that names none."""
    from ouroboros.tool_access_paths import canonical_data_root as _canonical

    metadata = getattr(ctx, "task_metadata", None)
    named = ((metadata.get("budget_drive_root") if isinstance(metadata, dict) else "")
             or getattr(ctx, "budget_drive_root", "") or getattr(ctx, "drive_root", ""))
    return _canonical(ctx) if str(named or "").strip() else None


def _raise(error: OSError) -> None:
    raise error


def executing_source_digest() -> str:
    """sha256 over every executing ``.py`` file's root-relative path and bytes."""
    import ouroboros

    root = pathlib.Path(ouroboros.__file__).resolve().parent.parent
    if not (root / "ouroboros" / "__init__.py").is_file():
        raise OSError(f"the executing package is not on disk under {root}")
    digest = hashlib.sha256()

    def add(relative: str, content: Optional[bytes]) -> None:
        digest.update(relative.encode("utf-8", "surrogateescape") + b"\0"
                      + (hashlib.sha256(content).digest() if content is not None else b"absent"))

    for name in _SOURCE_FILES:
        add(name, (root / name).read_bytes() if (root / name).is_file() else None)
    for tree in _SOURCE_TREES:
        if not (root / tree).is_dir():
            add(tree, None)
            continue
        for directory, subdirs, names in os.walk(root / tree, onerror=_raise):
            subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
            for name in sorted(names):
                if name.endswith(".py"):
                    path = pathlib.Path(directory, name)
                    add(path.relative_to(root).as_posix(), path.read_bytes())
    return digest.hexdigest()


def capture_executing_source_baseline() -> str:
    """Pin this process's executing source ONCE, where its code generation is loaded.

    Callers: ``server.py`` at import; the pool worker's entry, including a worker whose
    ``__main__`` imports no server; and the standalone review entry before the rest of
    the body loads. Whether a forkserver preloads the main module or a child re-imports
    it depends on the Python runtime and entrypoint, not the new child's PID. The first capture wins for the life of the memory image: a fork or
    forkserver child inherits the value with the modules it inherits and never recaptures
    because its PID changed. An unreadable inventory leaves this image permanently without
    a baseline — a later disk read is not the loaded generation.
    """
    global _baseline
    if _baseline is None:
        with _baseline_lock:
            if _baseline is None:
                try:
                    _baseline = executing_source_digest()
                except (OSError, ValueError):
                    log.warning("Executing-source baseline unavailable; durable test receipts are off",
                                exc_info=True)
                    _baseline = ""
    return _baseline


def _source_status() -> tuple[str, str]:
    """``(digest, "")`` while the disk holds the bytes this process loaded, else ``("", why)``."""
    if not _baseline:
        return "", "no executing-source baseline was captured where this process loaded its code"
    try:
        current = executing_source_digest()
    except (OSError, ValueError) as exc:
        return "", f"the executing source could not be re-read ({type(exc).__name__})"
    if current != _baseline:
        return "", "the executing source changed after this process loaded it"
    return current, ""


# The SELECTED test interpreter under the exact scrubbed child environment and import
# order (no -I/-E), from the gate's own temp root rather than the candidate tree.
# Metadata hashes catch same-version reinstalls; site-packages bytes are not rehashed,
# so a manual in-place edit absent from metadata stays a local-trust limit.
_INSTALLATION_PROBE = r"""
import hashlib, json, os, platform, site, sys, sysconfig
from importlib import metadata
temp = sys.argv[1]
def local(path):
    path = str(path)
    return "<preflight-temp>" + path[len(temp):] if path.startswith(temp) else path
def text_digest(text):
    return None if text is None else hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()
def file_digest(path):
    try:
        with open(path, "rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()
    except OSError:
        return None
dists, unidentified = [], []
for dist in metadata.distributions():
    where = local(getattr(dist, "_path", "") or dist.locate_file(""))
    meta, record, direct = (dist.read_text(name) for name in ("METADATA", "RECORD", "direct_url.json"))
    try:
        editable = bool((json.loads(direct) if direct else {}).get("dir_info", {}).get("editable"))
    except (ValueError, AttributeError):
        editable = True
    if editable or meta is None or record is None:
        unidentified.append(where)
    dists.append([where, text_digest(meta), text_digest(record), text_digest(direct)])
sites = [p for p in site.getsitepackages() + ([site.getusersitepackages()] if site.ENABLE_USER_SITE else [])
         if os.path.isdir(p)]
hooks = sorted([local(os.path.join(d, n)), file_digest(os.path.join(d, n))] for d in sites
               for n in os.listdir(d) if n.endswith(".pth") or n in ("sitecustomize.py", "usercustomize.py"))
print(json.dumps({
    "python": [sys.version, sys.implementation.name, sys.implementation.cache_tag, sys.platform,
               sysconfig.get_platform(), platform.platform()],
    "prefix": [sys.executable, sys.prefix, sys.base_prefix, sys.exec_prefix],
    "pyvenv_cfg": file_digest(os.path.join(sys.prefix, "pyvenv.cfg")),
    "path": [local(p) for p in sys.path[1:]],
    "distributions": dists, "site_hooks": hooks, "unidentified": unidentified,
}))
"""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _installation_identity(agent_python: str, env: dict, temp_root: pathlib.Path) -> tuple[Optional[dict], str]:
    """What the test interpreter loads and which Git the tests invoke, or ``(None, why)``.

    Physical paths stay in the identity, so another venv or a relocated (translocated)
    bundle is an intentional miss. ``git --version`` covers a shim whose own file never
    changes when the toolchain behind it does.
    """
    from ouroboros.commit_admission import _executable_identity

    try:
        probe = subprocess.run([agent_python, "-c", _INSTALLATION_PROBE, str(temp_root)], cwd=str(temp_root),
                               env=env, capture_output=True, text=True, timeout=120)
        if probe.returncode != 0:
            return None, f"the installation probe exited {probe.returncode}"
        installation = json.loads(probe.stdout)
        if installation["unidentified"]:
            return None, "editable or unidentifiable distributions: " + ", ".join(installation["unidentified"][:3])
        git = shutil.which("git", path=env.get("PATH"))
        if not git:
            return None, "no git on the test PATH"
        version = subprocess.run([git, "--version"], cwd=str(temp_root), env=env,
                                 capture_output=True, text=True, timeout=60)
        if version.returncode != 0:
            return None, f"git --version exited {version.returncode}"
        installation["git"] = [list(_executable_identity(git)), version.stdout.strip()]
        return installation, ""
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        return None, f"the installation could not be identified ({type(exc).__name__})"


_PROVENANCE = ("created_at", "task_id", "phase")


def _record_problem(document: Any, identity: dict, node_ran: bool) -> str:
    """Why a receipt record cannot stand for this identity's complete default run, or ``""``.

    Proportional coherence, not a forgery defence (the file is unsigned): the lanes must
    be the ones THIS identity's recipe requires — the node lane exactly when the candidate
    carries web tests, a non-empty parallel lane on two observed workers (or the one the
    budget admitted), the serial lane — and the record must keep its run's provenance.
    """
    if not isinstance(document, dict):
        return "not a JSON object"
    for key, expected in (("schema", RECEIPT_SCHEMA), ("identity", identity), ("result", "passed"),
                          ("authenticity", "unsigned")):
        if document.get(key) != expected:
            return f"{key} does not match"
    lanes, seconds = document.get("lanes"), document.get("pass_seconds")
    if not isinstance(lanes, dict) or lanes.get("node") != ("passed" if node_ran else "absent"):
        return "the node lane does not match the recipe this candidate requires"
    # The live proof's rule: two observed workers, or the one the memory budget admitted.
    workers, admitted = lanes.get("parallel_workers"), lanes.get("admitted_workers", 2)
    if (type(workers) is not int or type(admitted) is not int or admitted < 1
            or workers < min(2, admitted) or lanes.get("serial") not in ("passed", "empty")):
        return "the parallel/serial lane evidence is incomplete"
    if (not isinstance(seconds, dict) or set(seconds) != {"parallel", "serial"}
            or any(type(value) not in (int, float) or value < 0 for value in seconds.values())):
        return "the executed passes are not the parallel and serial lanes"
    if not all(isinstance(document.get(key), str) for key in _PROVENANCE) or not (
            document["created_at"] and document["phase"]):
        return "the original run's provenance is missing"
    return ""


class FullGateReceipt:
    """The durable identity of one complete default gate attempt, or why it has none.

    ``status`` discloses what happened (``declined``, ``absent``, ``unreadable``,
    ``invalid``, ``reused``, ``published``, ``unwritten``); it is diagnostics, and no
    status ever turns a red or skipped gate green. ``origin`` is the reused record's own
    provenance (when, by which task, in which phase that run happened) and
    ``empty_lanes`` the lanes that run observed collecting no test.
    """

    def __init__(self, path: Optional[pathlib.Path], identity: dict, reason: str = "", node_ran: bool = False) -> None:
        self.path, self.identity, self.reason, self.node_ran = path, identity, reason, node_ran
        self.status = "declined" if path is None else "unchecked"
        self.origin: dict = {}
        self.empty_lanes: Optional[list] = None
        self.evidence = None

    @classmethod
    def prepare(cls, ctx: Any, subject: Any, passes, probe_module: str, agent_python: str,
                temp_root: pathlib.Path, worktree: pathlib.Path) -> "FullGateReceipt":
        """Bind the identity before any lane runs; any missing fact declines durability only."""
        from ouroboros import preflight_runner as pr
        from ouroboros.commit_admission import workload_fingerprint

        if subject is None:
            return cls(None, {}, "no tested subject (no caller ctx, or the checkout could not be bound)")
        if [spec.label for spec in passes] != ["parallel", "serial"] or probe_module not in passes[0].args:
            return cls(None, {}, "not the complete default recipe")
        if subject.tier != "full":
            return cls(None, {}, "selected evidence cannot mint a FullGateReceipt")
        root = canonical_data_root(ctx)
        if root is None:
            return cls(None, {}, "no canonical data root")
        source, why = _source_status()
        if not source:
            return cls(None, {}, why)
        installation, why = _installation_identity(agent_python, pr._preflight_env(temp_root, worktree), temp_root)
        if installation is None:
            return cls(None, {}, why)
        identity = {"schema": RECEIPT_SCHEMA, "head": subject.head, "tree": subject.tree,
                    "index_tree": subject.index_tree, "workload": workload_fingerprint(subject.workload),
                    "installation": _digest(installation), "source": source}
        return cls(root / "state" / RECEIPTS_DIR / f"{_digest(identity)}.json", identity,
                   node_ran=bool(subject.workload[4]))

    @property
    def key(self) -> str:
        return self.path.stem if self.path is not None else ""

    def covered(self) -> bool:
        """Whether a coherent receipt of exactly this identity exists; any doubt reads as absent."""
        if self.path is None:
            return False
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.status = "absent"
            return False
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self.status, self.reason = "unreadable", type(exc).__name__
            log.warning("Preflight receipt %s is unreadable; running the gate", self.path, exc_info=True)
            return False
        problem = _record_problem(document, self.identity, self.node_ran)
        if problem:
            self.status, self.reason = "invalid", problem
            log.warning("Preflight receipt %s is not usable (%s); running the gate", self.path, problem)
            return False
        self.status, self.origin = "reused", {key: document[key] for key in _PROVENANCE}
        self.evidence = document.get("evidence")
        # Only a non-empty parallel lane is ever published; the serial lane may have been empty.
        self.empty_lanes = ["serial"] if document["lanes"]["serial"] == "empty" else []
        return True

    def publish(self, ctx: Any, temp_root: pathlib.Path, worktree: pathlib.Path, agent_python: str,
                empty_passes: list, phase: str, timings: list) -> None:
        """Write the receipt after a complete green run whose identity held throughout.

        Called only once every lane is green and containment proven. A source or
        installation that moved during the run, an empty parallel lane or fewer
        observed workers than the gate requires (two, or the one admitted) decline; a
        write failure is logged. Neither changes the verdict the runner returns.
        """
        from ouroboros import preflight_runner as pr
        from ouroboros.utils import atomic_write_json, utc_now_iso

        if self.path is None:
            return
        try:
            workers, admitted = len(pr._observed_worker_ids(temp_root)), pr._allocated_worker_count(temp_root)
            reason = ("the parallel pass collected no tests" if "parallel" in empty_passes else
                      f"{workers} observed xdist worker(s)" if workers < min(pr._MIN_PREFLIGHT_WORKERS, admitted) else "")
            if not reason:
                reason = _source_status()[1]
            if not reason:
                installation, reason = _installation_identity(
                    agent_python, pr._preflight_env(temp_root, worktree), temp_root)
                if installation is not None and _digest(installation) != self.identity["installation"]:
                    reason = "the installation changed during the run"
            if reason:
                self.status, self.reason = "declined", reason
                return
            self.evidence = getattr(ctx, "_preflight_test_evidence", None)
            atomic_write_json(self.path, {
                "schema": RECEIPT_SCHEMA, "identity": self.identity, "result": "passed",
                "authenticity": "unsigned",
                "lanes": {"node": "passed" if self.node_ran else "absent", "parallel_workers": workers,
                          "admitted_workers": admitted, "serial": "empty" if "serial" in empty_passes else "passed"},
                "pass_seconds": dict(timings), "phase": phase,
                "task_id": str(getattr(ctx, "task_id", "") or ""), "created_at": utc_now_iso(),
                "evidence": getattr(ctx, "_preflight_test_evidence", None),
            })
            self.status = "published"
        except Exception as exc:  # durability is an optimization; the verdict is already decided
            self.status, self.reason = "unwritten", f"{type(exc).__name__}: {exc}"
            log.warning("Preflight receipt %s could not be written", self.path, exc_info=True)

    def disclosure(self) -> dict:
        """The event-log projection: disclosure, never authority."""
        return {"status": self.status, "key": self.key, **({"reason": self.reason} if self.reason else {}),
                **({"evidence": self.evidence} if self.evidence else {}),
                **({"origin": dict(self.origin)} if self.origin else {})}


# ----------------------------------------------------------------- reviewer exhibit
# ``preflight_execution``: the gate's execution fact, frozen once per wave BEFORE any
# brief is sized or hashed, rendered inline to every seat, carried in each seat's
# persisted ``ReviewRequest.evidence`` and in the ledger record, and read back from
# that frozen request on a pending rejoin — never rebuilt from a later ctx. A wave
# nobody supplied one for (a standalone ``review_change``, a legacy pending wave)
# stays ``unobserved``. It enters no retry, reuse or contract identity.
EXHIBIT_KEY = "preflight_execution"
UNOBSERVED = {"status": "unobserved"}


def execution_exhibit(ctx: Any, *, otherwise: str = "") -> dict:
    """What the runner established for the candidate ``ctx.repo_dir`` holds NOW.

    ``passed`` only while the runner's process-held proof still covers the live
    candidate (the ``commit_gate._review_tests_facts`` rule), naming the tested
    HEAD/tree/index, the lanes and whether this process executed them, reused an
    identical earlier run of this process or reused an earlier run's unsigned
    receipt; anything else is ``not_run`` with the caller's reason (``otherwise``),
    else its own: a completed run may have minted no proof of the tested subject.
    ``lanes`` are the lanes the proof's run observed running tests, ``empty_lanes``
    those it observed collecting none; an undisclosed outcome names no lane, since
    the recipe says which lanes were started, not which ran a test.
    """
    from ouroboros.commit_admission import PreflightTestProof, preflight_test_proof_matches

    proof = getattr(ctx, "_preflight_test_proof", None)
    completed = getattr(ctx, "_preflight_tests_passed", None) is True
    if not completed or not isinstance(proof, PreflightTestProof):
        return {"status": "not_run", "reason": otherwise or (
            "the run completed but minted no proof of the tested subject" if completed
            else "no green hermetic run (disabled or no applicable suite)")}
    try:
        bound = preflight_test_proof_matches(ctx, ctx.repo_dir, require_full=False)
    except Exception:
        bound = False
    if not bound:
        return {"status": "not_run", "reason": "the green run's proof does not cover this candidate"}
    event = dict(getattr(ctx, "_preflight_test_disclosure", None) or {})
    if (event.get("head"), event.get("tree"), event.get("index_tree")) != (proof.head, proof.tree, proof.index_tree):
        event = {}
    receipt = dict(event.get("receipt") or {})
    evidence = ("receipt" if receipt.get("status") == "reused" else "process_proof"
                if event.get("action") == "reused" else "executed" if event else "undisclosed")
    recipe = (["node"] if proof.workload[4] else []) + [str(spec[0]) for spec in proof.workload[0]]
    empty = event.get("empty_lanes")
    empty = [lane for lane in recipe if lane in empty] if isinstance(empty, list) else None
    return {"status": "passed", "authenticity": "unsigned", "evidence": evidence, "tier": proof.tier,
            **{key: event[key] for key in ("base", "policy", "rule", "files", "reasons", "extra_tests",
                                           "full_tests", "test_reason", "expected_files", "omitted_files", "inventory") if key in event},
            "lanes": [lane for lane in recipe if lane not in empty] if empty is not None else [],
            **({"empty_lanes": empty} if empty else {}),
            "tested": {"head": proof.head, "tree": proof.tree, "index_tree": proof.index_tree},
            **({"receipt": receipt["key"]} if receipt.get("status") in ("reused", "published") else {}),
            **({"receipt_origin": dict(receipt["origin"])} if evidence == "receipt" and receipt.get("origin") else {})}


def bind_execution_exhibit(exhibit: Any, frozen: Any) -> dict:
    """A caller-supplied exhibit for one ``review_change`` subject: kept when its tested
    checkout IS the frozen subject (HEAD = the materialized parent, tree = the subject
    tree — a base..head proposal is tested on its base, never on its own head)."""
    if not isinstance(exhibit, dict) or not exhibit:
        return dict(UNOBSERVED)
    tested = exhibit.get("tested") or {}
    if exhibit.get("status") == "passed" and (tested.get("head"), tested.get("tree")) != (
            str(getattr(frozen, "parent_sha", "") or ""), str(getattr(frozen, "tree_sha", "") or "")):
        return {"status": "not_run", "reason": "the supplied test execution names another subject"}
    return dict(exhibit)


def execution_section(exhibit: Any) -> str:
    """The seat-visible text of a frozen exhibit; ``""`` when none was supplied."""
    exhibit = exhibit if isinstance(exhibit, dict) else {}
    status = exhibit.get("status")
    if status == "passed":
        tested = exhibit.get("tested") or {}
        how = {"executed": "executed by the hermetic runner in this process",
               "process_proof": "this process's earlier proof of the identical subject, not re-executed now",
               # The origin strings come from the unsigned file: JSON-quoted, they stay data.
               "receipt": ("an unsigned receipt " + str(exhibit.get("receipt") or "")[:16]
                           + " of one earlier run (" + ", ".join(
                               f"{key} {json.dumps(value)}"
                               for key, value in (exhibit.get("receipt_origin") or {}).items() if value)
                           + "): historical, not re-executed now"),
               }.get(str(exhibit.get("evidence")), "not disclosed")
        lanes, empty = exhibit.get("lanes") or [], exhibit.get("empty_lanes") or []
        observed = (f": lanes {', '.join(lanes)}, green with proven process containment" if lanes
                    else "; which of its lanes ran tests is not disclosed")
        if empty:
            observed += f"; lanes {', '.join(empty)} collected no tests, so no test ran there"
        scope = ""
        if exhibit.get("tier") == "selected" and isinstance(exhibit.get("files"), list):
            scope = (f" over {len(exhibit['files'])} selected test modules ({exhibit.get('omitted_files', 0)} "
                     f"not run; {exhibit.get('rule', 'floor, direct importers and widening')})")
        return ("## Preflight test execution (host fact)\n"
                f"The {exhibit.get('tier', 'full')} hermetic test gate passed{scope}{observed}.\n"
                f"Tested checkout: HEAD {tested.get('head')}, tree {tested.get('tree')}, index "
                f"{tested.get('index_tree')}. Evidence: {how}.\n"
                "Unsigned host fact, not a verdict: decide yourself which further checks this change needs.")
    if status == "not_run":
        return ("## Preflight test execution (host fact)\n"
                f"NOT_RUN: {exhibit.get('reason') or 'no hermetic run covers this candidate'}. "
                "No passing test result is claimed for this candidate.")
    return ""
