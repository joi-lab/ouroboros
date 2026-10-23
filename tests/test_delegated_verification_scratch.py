"""A delegated session that changes and checks code has a lawful place for each kind of write.

Work product stays in the private copy; disposable process state goes outside every Git
working tree, where safe_test's own guard accepts it; named evidence goes to the copy's
``.review-evidence/``, which the host retains before any disposition releases the copy and
keeps out of the product patch (#2070). Read-only access is unchanged.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

from ouroboros import delegate_custody as custody
from ouroboros.delegate_start_instructions import (
    ACCESS_INSTRUCTIONS,
    UNPROVEN_BOUNDARY_INSTRUCTION,
    execution_binding_text,
    scratch_fact,
)
from ouroboros.subagent_worktrees import find_execution_snapshot, provision_execution_snapshot
from ouroboros.workspace_patch_rules import NAMED_EVIDENCE_EXCLUSION
from tests.test_delegated_run_isolation import _git, _isolated_entry, _nanny_ctx, _seed_target

REPO = pathlib.Path(__file__).resolve().parents[1]
_LAUNCHER = [sys.executable, "-I", "-S", str(REPO / "scripts" / "safe_test.py")]


def test_readonly_access_line_is_unchanged_and_writable_lines_name_one_outside_place():
    assert ACCESS_INSTRUCTIONS["readonly"].startswith(
        " ACCESS: you may read and run read-only commands inside this root, and make no edits or writes; ")
    assert "Git" not in ACCESS_INSTRUCTIONS["readonly"]  # a read-only session gains no write place
    write = ACCESS_INSTRUCTIONS["workspace_write"]
    assert "must not write outside this root" not in write
    assert "outside it write only disposable process state outside every Git working tree" in write
    assert "never another project or the operator's files" in write
    assert "stay inside this root" not in UNPROVEN_BOUNDARY_INSTRUCTION
    assert "disposable process state outside every Git working tree" in UNPROVEN_BOUNDARY_INSTRUCTION
    # The durable shared download cache is not disposable state: it is allowed by exact host naming only.
    for rule in (write, UNPROVEN_BOUNDARY_INSTRUCTION, execution_binding_text("/copy", "/authority")):
        assert "the shared download cache your host names" in rule
    assert "beyond the paths your host names" in UNPROVEN_BOUNDARY_INSTRUCTION


def test_a_payload_copy_is_told_it_has_no_evidence_folder():
    text = execution_binding_text("/snaps/dlgp_t_x", "/data/skills/external/alpha", evidence_root="")
    assert ".review-evidence" not in text and "safe_test" not in text
    assert "every file you leave in it becomes part of what is captured" in text
    assert "no evidence folder is retained for this run" in text and scratch_fact() in text


@pytest.mark.parametrize("investigation", [False, True])
def test_blocked_checks_return_to_the_integrator_without_widening_access(investigation):
    from ouroboros.delegate_start_instructions import access_instruction

    readonly = access_instruction("readonly", investigation=investigation)
    assert readonly == ACCESS_INSTRUCTIONS["readonly"]
    for access in ("workspace_write", "full"):
        text = access_instruction(access, investigation=investigation)
        assert text.startswith(ACCESS_INSTRUCTIONS[access])
        assert "report it as unverified" in text and "do not widen access or weaken the check" in text
        assert "under its own authority" in text
        assert ("PURPOSE: this is an evidence-only investigation" in text) is investigation


def test_a_folder_investigation_copy_keeps_its_evidence_channel_and_a_folder_product_none():
    """A non-Git copy is captured whole. An evidence-only investigation's copy IS its retained evidence
    (its PURPOSE line names .review-evidence/), so its binding never denies that folder; a folder
    product's copy still retains none."""
    from ouroboros.delegate_start_instructions import INVESTIGATION_INSTRUCTION, directory_copy_binding_instruction

    investigation = directory_copy_binding_instruction("/authority", investigation=True)
    assert "no evidence folder" not in investigation and "every file you leave in it" not in investigation
    assert f"outside this copy goes outside every Git working tree ({scratch_fact()})" in investigation
    assert "under .review-evidence/: the host retains that folder" in INVESTIGATION_INSTRUCTION
    product = directory_copy_binding_instruction("/authority")
    assert "no evidence folder is retained for this run" in product and "safe_test" not in product


def test_the_named_scratch_parent_is_the_one_safe_test_accepts_and_an_in_git_parent_is_named(
        tmp_path, monkeypatch):
    """The fact and the guard share one predicate: what the binding names, the launcher accepts,
    and the parent it refuses is named as refused instead of offered. Only the body's launcher
    fact names safe_test; any other project hears the same place without a command it lacks."""
    from ouroboros import test_environment

    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(test_environment, "default_temp_parent", lambda: plain)
    assert scratch_fact(launcher=True) == f"safe_test's default temp parent on this host is {plain}"
    assert scratch_fact() == f"for example under {plain}"
    probe = [sys.executable, "-c", "print('ran')"]
    accepted = subprocess.run(_LAUNCHER + ["--temp-parent", str(plain), "--evidence-dir", str(tmp_path / "evidence-ok"),
                                           "--"] + probe, cwd=REPO, text=True, capture_output=True, timeout=120)
    assert accepted.returncode == 0 and "ran" in accepted.stdout, accepted.stderr

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    nested = checkout / "tmp"
    nested.mkdir()
    monkeypatch.setattr(test_environment, "default_temp_parent", lambda: nested)
    fact, generic = scratch_fact(launcher=True), scratch_fact()
    assert f"inside the Git working tree {checkout.resolve()}" in fact and "--temp-parent" in fact
    assert (f"{nested} is inside the Git working tree {checkout.resolve()}: choose a place outside every "
            "Git working tree") in generic and "safe_test" not in generic
    refused = subprocess.run(_LAUNCHER + ["--temp-parent", str(nested), "--evidence-dir", str(tmp_path / "evidence-no"),
                                          "--"] + probe, cwd=REPO, text=True, capture_output=True, timeout=120)
    assert refused.returncode == 2 and "must not be inside a Git working tree" in refused.stderr
    assert "ran" not in refused.stdout and list(nested.iterdir()) == []
    assert not (tmp_path / "evidence-no").exists()  # refused before anything was written


def test_safe_test_writes_named_evidence_into_a_checkout_while_scratch_stays_outside(tmp_path):
    """The evidence destination may sit inside a Git copy; only the disposable root may not."""
    copy = tmp_path / "copy"
    subprocess.run(["git", "init", str(copy)], check=True, capture_output=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    evidence = copy / ".review-evidence"
    result = subprocess.run(_LAUNCHER + ["--temp-parent", str(scratch), "--evidence-dir", str(evidence), "--",
                                         sys.executable, "-c", "print('checked')"],
                            cwd=REPO, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    [run] = list(evidence.iterdir())
    record = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert record["returncode"] == 0 and (run / "OUROBOROS_TEST_EVIDENCE_RUN.txt").is_file()
    assert pathlib.Path(record["scratch"]).parent == scratch.resolve()  # the disposable root stayed outside Git
    final = result.stderr.splitlines()[-1].split(" ", 1)
    assert final[0] in ("SAFE_TEST_RELEASED", "SAFE_TEST_RETAINED") and pathlib.Path(final[1]).parent == scratch.resolve()


def _snapshot(tmp_path, monkeypatch, *, tracked_evidence=False, baseline_link=""):
    target = _seed_target(tmp_path)
    if tracked_evidence:
        (target / ".review-evidence").mkdir()
        (target / ".review-evidence" / "keep.txt").write_text("the project's own file\n", encoding="utf-8")
        if baseline_link:
            _symlink_or_skip(target / ".review-evidence" / "project-link", "../tracked.txt")
            if baseline_link == "root":
                shutil.rmtree(target / ".review-evidence")
                _symlink_or_skip(target / ".review-evidence", "project-logs", directory=True)
                (target / "project-logs").mkdir()
                (target / "project-logs" / "not-evidence.txt").write_text("product\n", encoding="utf-8")
        _git(target, "add", ".review-evidence")
        if baseline_link == "root":
            _git(target, "add", "project-logs")
        _git(target, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "tracked evidence")
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapE")
    custody._CUSTODY.clear()
    return target, ctx, handle, pathlib.Path(handle.path)


def _write_evidence(copy):
    run = copy / ".review-evidence" / "run-1"
    run.mkdir(parents=True)
    # Retention preserves bytes: exercise both LF and CRLF on every host.
    expected = {".review-evidence/run-1/result.json": b'{"returncode": 0}\n',
                ".review-evidence/notes.txt": b"checked with the fixture\r\n"}
    for name, payload in expected.items():
        (copy / name).write_bytes(payload)
    return expected


def _retained(block):
    root = pathlib.Path(block["named_evidence"]["evidence_dir"]) / "files"
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("decision", ["apply", "reject"])
def test_candidate_evidence_is_retained_out_of_the_patch_before_either_disposition(tmp_path, monkeypatch, decision):
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch)
    expected = _write_evidence(copy)
    (copy / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        assert capture["status"] == "ready_with_changes", capture
        manifest = json.loads(pathlib.Path(capture["manifest_artifact"]).read_text(encoding="utf-8"))
        assert {row["path"]: row["reason"] for row in manifest["untracked_excluded"]
                if row["path"] in expected} == dict.fromkeys(expected, NAMED_EVIDENCE_EXCLUSION)
        patch = pathlib.Path(capture["patch_artifact"]).read_text(encoding="utf-8")
        assert "feature.py" in patch and ".review-evidence" not in patch
        assert capture["named_evidence"]["status"] == "retained" and _retained(capture) == expected
        assert not entry.patch_disposed  # a candidate's retention never decides for its owner
        out = _integrate_delegated_patch(ctx, "run-1", decision, "checked")
        assert ("✅ Integrated" if decision == "apply" else "🚫 Rejected") in out, out
        assert "named evidence folder are evidence, not part of this patch" in out
        assert "EXCLUDED from this patch" not in out  # evidence is no lost deliverable
        assert (target / "feature.py").exists() is (decision == "apply")
        assert not (target / ".review-evidence").exists()  # never applied
        assert not copy.exists() and find_execution_snapshot("snapE") is None
        assert _retained(capture) == expected  # survives the released copy
    finally:
        custody._CUSTODY.clear()


def _saved(capture):
    return json.loads((pathlib.Path(capture["named_evidence"]["evidence_dir"]) / "summary.json").read_text(
        encoding="utf-8"))


def _symlink_or_skip(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # e.g. Windows without the symlink privilege
        pytest.skip(f"symlinks unavailable here: {exc}")


@pytest.mark.parametrize("decision", ["apply", "reject"])
def test_a_retention_gap_withholds_the_disposition_until_nothing_unretained_is_left(tmp_path, monkeypatch, decision):
    """No disposition may release the last copy of named evidence: a gap refuses it, so the copy stays
    settled and undisposed, the state custody's existing keep-set (``open_snapshot_ids``) holds after
    the task ends too. The refusal's own remedy, removing what is not evidence, then lets the same call
    dispose and release: an old gap never holds a surviving copy forever."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch)
    (copy / ".review-evidence").write_text("a file where the folder belongs\n", encoding="utf-8")
    (copy / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        gaps = [{"path": ".review-evidence", "reason": "evidence_root_not_directory"}]
        assert capture["named_evidence"]["status"] == "partial" and capture["named_evidence"]["gaps"] == gaps
        out = _integrate_delegated_patch(ctx, "run-1", decision, "checked")
        assert out.startswith("⚠️ INTEGRATE_DELEGATED_EVIDENCE_UNRETAINED"), out
        assert "(partial: .review-evidence)" in out and "No disposition was recorded" in out
        assert entry.patch_disposed == "" and not (target / "feature.py").exists()  # nothing applied
        assert find_execution_snapshot("snapE") is not None and (copy / ".review-evidence").is_file()
        (copy / ".review-evidence").unlink()  # remove what is not evidence
        out = _integrate_delegated_patch(ctx, "run-1", decision, "checked")
        assert ("✅ Integrated" if decision == "apply" else "🚫 Rejected") in out, out
        assert (target / "feature.py").exists() is (decision == "apply") and not copy.exists()
        # The record keeps what it said, the gap moved aside: nothing named is left in the copy to lose.
        assert _saved(capture) == {**capture["named_evidence"], "status": "retained", "gaps": [], "removed_gaps": gaps}
    finally:
        custody._CUSTODY.clear()


@pytest.mark.parametrize("removed", ["link", "folder", "linked_folder"])
def test_removing_unretainable_evidence_releases_the_copy_and_keeps_what_was_retained(tmp_path, monkeypatch, removed):
    """Retention never follows a link, so a link is a gap. Removing that entry, the whole folder, or the
    linked folder itself leaves nothing unretained in the surviving copy: the same reject disposes, and
    files retained before stay listed, readable and byte-exact (never the link's target)."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.log").write_text("not this copy's\n", encoding="utf-8")
    if removed == "linked_folder":
        _symlink_or_skip(copy / ".review-evidence", outside, directory=True)
        expected, gap = {}, ".review-evidence"
    else:
        expected, gap = _write_evidence(copy), ".review-evidence/link.log"
        _symlink_or_skip(copy / gap, outside / "elsewhere.log")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        gaps = [{"path": gap, "reason": "evidence_symlink"}]
        assert capture["named_evidence"]["status"] == "partial" and capture["named_evidence"]["gaps"] == gaps
        assert _retained(capture) == expected
        out = _integrate_delegated_patch(ctx, "run-1", "reject", "checked")
        assert out.startswith("⚠️ INTEGRATE_DELEGATED_EVIDENCE_UNRETAINED") and gap in out, out
        if removed == "folder":
            shutil.rmtree(copy / ".review-evidence")
        else:
            (copy / gap).unlink()  # the link itself, never its target
        out = _integrate_delegated_patch(ctx, "run-1", "reject", "checked")
        assert "🚫 Rejected" in out and not copy.exists(), out
        saved = _saved(capture)
        assert saved["status"] == "retained" and saved["gaps"] == [] and _retained(capture) == expected
        assert {row["path"] for row in saved["files"]} == set(expected) and (outside / "elsewhere.log").exists()
        assert saved.get("removed_gaps", []) == ([] if removed == "link" else gaps)  # a re-read folder has none
    finally:
        custody._CUSTODY.clear()


def test_a_gone_copy_is_not_held_by_its_last_evidence_record(tmp_path, monkeypatch):
    """A copy that no longer exists is the last copy of nothing: its partial record stays exactly as
    written, and the disposition proceeds instead of refusing forever."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch)
    (copy / ".review-evidence").write_text("a file where the folder belongs\n", encoding="utf-8")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        assert capture["named_evidence"]["status"] == "partial"
        record = pathlib.Path(capture["named_evidence"]["evidence_dir"]) / "summary.json"
        written = record.read_bytes()
        shutil.rmtree(copy)  # lost outside the host, unlike a removal inside a surviving copy
        out = _integrate_delegated_patch(ctx, "run-1", "reject", "checked")
        assert "🚫 Rejected" in out and "INTEGRATE_DELEGATED_EVIDENCE_UNRETAINED" not in out, out
        assert record.read_bytes() == written
    finally:
        custody._CUSTODY.clear()


def test_full_suite_sized_evidence_is_retained_whole_by_streaming(tmp_path, monkeypatch):
    """A candidate's named evidence is retained whole, as a reviewer seat's: past the 64 MiB an
    investigation is bounded by, byte-exact and copied as a stream, so a full verification run never
    refuses its disposition for size, and no file is read into memory or into any answer."""
    import hashlib
    import tracemalloc

    from ouroboros import review_checking
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch)
    big = copy / ".review-evidence" / "run-1" / "results.jsonl"
    big.parent.mkdir(parents=True)
    chunk, digest = (b"x" * 1023 + b"\n") * 1024, hashlib.sha256()  # one MiB of result lines
    with big.open("wb") as stream:
        for _ in range(review_checking.EVIDENCE_RETAIN_BYTES // len(chunk) + 1):
            stream.write(chunk)
            digest.update(chunk)
    size = big.stat().st_size
    assert size > review_checking.EVIDENCE_RETAIN_BYTES
    entry = _isolated_entry(ctx, target, handle)
    try:
        tracemalloc.start()
        try:
            capture = _capture_terminal_patch(ctx, entry)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        named = capture["named_evidence"]
        assert named["status"] == "retained" and named["gaps"] == [], named
        assert named["files"] == [{"path": ".review-evidence/run-1/results.jsonl", "size": size,
                                   "sha256": digest.hexdigest()}]
        assert peak < 16 * 1024 * 1024, peak  # streamed: the file never sat in memory whole
        out = _integrate_delegated_patch(ctx, "run-1", "reject", "checked")
        assert "🚫 Rejected" in out and not copy.exists() and len(out) < 4096, out[:400]
        kept = pathlib.Path(named["evidence_dir"]) / "files" / ".review-evidence" / "run-1" / "results.jsonl"
        assert kept.stat().st_size == size
    finally:
        custody._CUSTODY.clear()


@pytest.mark.parametrize("recorded", ["staged", "committed"])
def test_new_evidence_staged_or_committed_in_the_copy_never_reaches_the_target(tmp_path, monkeypatch, recorded):
    """``git add -A``, or a local commit (disclosed as head_moved), makes new evidence tracked in the copy
    but never in the recorded baseline: it stays out of the patch and is retained beside it, while the
    product change next to it applies."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch)
    expected = _write_evidence(copy)
    (copy / "feature.py").write_text("VALUE = 5\n", encoding="utf-8")
    _git(copy, "add", "-A")
    if recorded == "committed":
        _git(copy, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "local")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        assert capture["status"] == "ready_with_changes", capture
        manifest = json.loads(pathlib.Path(capture["manifest_artifact"]).read_text(encoding="utf-8"))
        assert {row["path"]: row["reason"] for row in manifest["tracked_excluded"]
                if row["path"] in expected} == dict.fromkeys(expected, NAMED_EVIDENCE_EXCLUSION)
        assert "feature.py" in manifest["tracked_changed"] and not set(expected) & set(manifest["tracked_changed"])
        patch = pathlib.Path(capture["patch_artifact"]).read_text(encoding="utf-8")
        assert "feature.py" in patch and ".review-evidence" not in patch
        assert ("head_moved" in capture) is (recorded == "committed")
        assert capture["named_evidence"]["status"] == "retained" and _retained(capture) == expected
        out = _integrate_delegated_patch(ctx, "run-1", "apply", "checked")
        assert "✅ Integrated" in out and "named evidence folder are evidence, not part of this patch" in out, out
        assert (target / "feature.py").read_text(encoding="utf-8") == "VALUE = 5\n"
        assert not (target / ".review-evidence").exists()
        assert ".review-evidence" not in _git(target, "status", "--porcelain", "--untracked-files=all").stdout
    finally:
        custody._CUSTODY.clear()


def test_evidence_written_after_a_retained_summary_is_retained_before_release(tmp_path, monkeypatch):
    """A retained summary stands only for the bytes it saw. A continued session writing into the same
    copy (modelled at the retention owner on one capture, not through continue_from) gets its new and
    rewritten evidence retained before the disposition releases the copy."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch)
    expected = _write_evidence(copy)
    (copy / "feature.py").write_text("VALUE = 4\n", encoding="utf-8")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        assert capture["named_evidence"]["status"] == "retained" and capture["named_evidence"]["witness"]
        (copy / ".review-evidence" / "later.log").write_bytes(b"written after the first retention\r\n")
        (copy / ".review-evidence" / "notes.txt").write_bytes(b"rewritten later\n")
        expected.update({".review-evidence/later.log": b"written after the first retention\r\n",
                         ".review-evidence/notes.txt": b"rewritten later\n"})
        out = _integrate_delegated_patch(ctx, "run-1", "reject", "checked")
        assert "🚫 Rejected" in out and not copy.exists(), out
        assert _retained(capture) == expected
    finally:
        custody._CUSTODY.clear()


def test_a_verification_only_run_releases_after_its_evidence_is_retained(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch)
    expected = _write_evidence(copy)
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        assert capture["status"] == "ready_no_changes", capture  # evidence is not a product change
        out = _integrate_delegated_patch(ctx, "run-1", "apply", "")
        assert "no captured file changes" in out and "the snapshot is released" in out, out
        assert not copy.exists() and _retained(capture) == expected
    finally:
        custody._CUSTODY.clear()


@pytest.mark.parametrize("staged", [False, True])
def test_tracked_files_under_the_evidence_folder_stay_in_the_product_patch(tmp_path, monkeypatch, staged):
    """A path the recorded baseline holds there is the project's own file: edited, staged or not, it
    stays work product and applies; only a file new since that baseline is evidence."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch, tracked_evidence=True)
    (copy / ".review-evidence" / "keep.txt").write_text("the project's own file, edited\n", encoding="utf-8")
    (copy / ".review-evidence" / "new.log").write_bytes(b"session log\r\n")
    if staged:
        _git(copy, "add", "-A")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        manifest = json.loads(pathlib.Path(capture["manifest_artifact"]).read_text(encoding="utf-8"))
        assert ".review-evidence/keep.txt" in manifest["tracked_changed"]
        assert ".review-evidence/new.log" not in manifest["tracked_changed"]
        excluded = {"path": ".review-evidence/new.log", "reason": NAMED_EVIDENCE_EXCLUSION}
        assert excluded in manifest["tracked_excluded" if staged else "untracked_excluded"]
        patch = pathlib.Path(capture["patch_artifact"]).read_text(encoding="utf-8")
        assert "edited" in patch and "session log" not in patch
        assert _retained(capture) == {".review-evidence/new.log": b"session log\r\n"}
        out = _integrate_delegated_patch(ctx, "run-1", "apply", "checked")
        assert "✅ Integrated" in out, out
        assert (target / ".review-evidence" / "keep.txt").read_text(encoding="utf-8") == "the project's own file, edited\n"
        assert not (target / ".review-evidence" / "new.log").exists()
    finally:
        custody._CUSTODY.clear()


@pytest.mark.parametrize("decision", ["apply", "reject"])
@pytest.mark.parametrize("baseline_link", ["entry", "root"])
def test_baseline_evidence_symlinks_are_product_and_do_not_block_unrelated_work(
        tmp_path, monkeypatch, decision, baseline_link):
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch, tracked_evidence=True, baseline_link=baseline_link)
    (copy / "feature.py").write_text("VALUE = 7\n", encoding="utf-8")
    expected = {}
    if baseline_link == "entry":
        (copy / ".review-evidence" / "check.log").write_bytes(b"new evidence\r\n")
        expected[".review-evidence/check.log"] = b"new evidence\r\n"
        # An edited baseline symlink is still product; retention never reads either target.
        link = copy / ".review-evidence" / "project-link"
        link.unlink()
        _symlink_or_skip(link, "../feature.py")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        assert capture["status"] == "ready_with_changes", capture
        assert capture["named_evidence"]["status"] == "retained", capture
        assert capture["named_evidence"]["gaps"] == [] and _retained(capture) == expected
        patch = pathlib.Path(capture["patch_artifact"]).read_text(encoding="utf-8")
        assert "feature.py" in patch
        if baseline_link == "entry":
            assert "project-link" in patch and "check.log" not in patch
        out = _integrate_delegated_patch(ctx, "run-1", decision, "checked")
        assert ("✅ Integrated" if decision == "apply" else "🚫 Rejected") in out, out
        assert (target / "feature.py").exists() is (decision == "apply")
        assert not copy.exists() and _retained(capture) == expected
        link = target / ".review-evidence" if baseline_link == "root" else target / ".review-evidence/project-link"
        # With core.symlinks=false (Git for Windows' default on hosted runners) Git writes an
        # applied link edit as a plain file holding its target; the edit is still product.
        plain = (decision == "apply" and baseline_link == "entry" and _git(
            target, "config", "--type=bool", "--get", "core.symlinks", check=False).stdout.strip() == "false")
        assert link.is_symlink() is not plain
        if baseline_link == "entry":
            text = link.read_text(encoding="utf-8") if plain else link.readlink().as_posix()
            assert text == ("../feature.py" if decision == "apply" else "../tracked.txt")
    finally:
        custody._CUSTODY.clear()


def test_a_baseline_root_link_is_never_walked_for_evidence_or_its_witness(tmp_path, monkeypatch):
    from ouroboros import review_checking as rc

    area, outside = tmp_path / "area", tmp_path / "outside"
    area.mkdir()
    outside.mkdir()
    _symlink_or_skip(area / ".review-evidence", outside, directory=True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an evidence root symlink must not be walked")

    monkeypatch.setattr(rc.os, "walk", forbidden)
    gaps = []
    assert rc.named_evidence_witness(area)
    assert rc.retain_named_evidence(area, tmp_path / "kept", [100], gaps,
                                     product_paths={".review-evidence"}) == []
    assert gaps == [] and not (tmp_path / "kept").exists()


def test_a_run_that_named_no_evidence_shows_none(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch

    target, ctx, handle, copy = _snapshot(tmp_path, monkeypatch)
    (copy / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        assert capture["status"] == "ready_with_changes" and "named_evidence" not in capture
        assert not (pathlib.Path(capture["manifest_artifact"]).parent / "evidence").exists()
    finally:
        custody._CUSTODY.clear()


def test_a_writable_reviewer_copy_names_the_same_scratch_place():
    from ouroboros.review_checking import checking_instructions

    text = checking_instructions("/seats/x/area", {"kind": "git_tree", "tree": "t", "parent": "p", "root": "/repo"})
    assert ".review-evidence/" in text and "outside every Git working tree" in text and scratch_fact() in text
    assert "safe_test" not in text  # the body's launcher is named only in a body seat's environment facts
