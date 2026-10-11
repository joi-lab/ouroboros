"""Snapshot working bytes stay exact; binary/large preimages stay outside Git's ODB."""
from hashlib import sha256
from pathlib import Path
import subprocess
import zipfile

import pytest

from ouroboros.artifacts import stream_artifact_file
from ouroboros.subagent_worktrees import find_execution_snapshot, provision_execution_snapshot
from ouroboros.workspace_file_outputs import file_output_changes, prepare_file_outputs
from ouroboros.workspace_patch_capture import write_workspace_patch_artifacts


def git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True)


def target_tree(tmp_path, geometry="ordinary"):
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init")
    (root / "code.txt").write_text("committed\n")
    git(root, "add", "code.txt")
    if geometry != "unborn":
        git(root, "-c", "user.name=Fixture", "-c", "user.email=f@invalid", "commit", "-m", "base")
    if geometry == "linked":
        linked = tmp_path / "linked"
        git(root, "worktree", "add", "--detach", str(linked))
        root = linked
    (root / "code.txt").write_text("staged\n")
    git(root, "add", "code.txt")
    (root / "code.txt").write_text("unstaged\n")
    return root


def document(path, text="Original document"):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')


def snapshot(tmp_path, target):
    return provision_execution_snapshot(
        target_root=target, task_id="input-test", snapshot_id="input-snapshot",
        worktree_root=tmp_path / "snapshots", data_dir=tmp_path / "data",
    )


def capture(tmp_path, handle):
    _, manifest = write_workspace_patch_artifacts(
        Path(handle.path), tmp_path / "capture", task={"metadata": {
            "workspace_preflight": {"git": {"head": handle.baseline_sha}},
            "file_baseline": handle.file_baseline,
        }},
    )
    assert not manifest["errors"], manifest["errors"]
    return manifest, file_output_changes(manifest, tmp_path / "capture")


@pytest.mark.parametrize("geometry", ["ordinary", "unborn", "linked"])
@pytest.mark.parametrize("operation", ["modify", "delete", "unchanged"])
def test_document_input_snapshot_capture_and_apply(tmp_path, geometry, operation):
    target = target_tree(tmp_path, geometry)
    document(target / "input.docx")
    original = (target / "input.docx").read_bytes()
    blob = git(target, "hash-object", "input.docx").stdout.decode().strip()
    before_status = git(target, "status", "--porcelain=v1", "-z").stdout
    before_index = git(target, "ls-files", "--stage", "-z").stdout
    before_head = git(target, "rev-parse", "--verify", "HEAD", check=False).stdout
    handle = snapshot(tmp_path, target)
    execution = Path(handle.path)
    assert (execution / "input.docx").read_bytes() == original
    assert git(target, "cat-file", "-e", blob, check=False).returncode != 0
    assert not git(execution, "ls-tree", "HEAD", "--", "input.docx").stdout
    assert git(target, "status", "--porcelain=v1", "-z").stdout == before_status
    assert git(target, "ls-files", "--stage", "-z").stdout == before_index
    assert git(target, "rev-parse", "--verify", "HEAD", check=False).stdout == before_head
    record = find_execution_snapshot(handle.snapshot_id, tmp_path / "data")
    assert record["file_baseline"] == handle.file_baseline
    before = record["file_baseline"]["input.docx"]
    assert before["size"] == len(original) and before["sha256"] == sha256(original).hexdigest()
    if operation == "modify":
        document(execution / "input.docx", "Changed document")
    elif operation == "delete":
        (execution / "input.docx").unlink()
    manifest, rows = capture(tmp_path, handle)
    assert manifest["patch_size"] == 0
    if operation == "unchanged":
        assert manifest["status"] == "ready_no_changes" and rows == []
        assert manifest["file_outputs"] == []
    else:
        assert manifest["status"] == "ready_with_changes"
        assert len(rows) == 1 and rows[0]["before"] == before
        with prepare_file_outputs(rows, target, baseline_sha=handle.baseline_sha,
                                  file_baseline=record["file_baseline"]) as prepared:
            prepared.apply()
            if operation == "modify":
                assert (target / "input.docx").read_bytes() == (execution / "input.docx").read_bytes()
            else:
                assert not (target / "input.docx").exists()
            prepared.rollback()
    assert (target / "input.docx").read_bytes() == original
    assert git(target, "ls-files", "--stage", "-z").stdout == before_index
    assert git(target, "status", "--porcelain=v1", "-z").stdout == before_status
    assert find_execution_snapshot(handle.snapshot_id, tmp_path / "data") is not None
    assert execution.exists()


def test_large_text_input_is_streamed_and_small_postimage_keeps_exact_preimage(tmp_path, monkeypatch):
    target = target_tree(tmp_path)
    input_path = target / "input.txt"
    with input_path.open("wb") as stream:
        for _ in range(51):
            stream.write(b"large input data\n" * 65536)
    expected = stream_artifact_file(input_path)
    original_read = Path.read_bytes
    def no_large_read(path):
        assert path.stat().st_size <= 50 * 1024 * 1024, "large input read into one bytes object"
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", no_large_read)
    blob = git(target, "hash-object", "input.txt").stdout.decode().strip()
    handle = snapshot(tmp_path, target)
    assert stream_artifact_file(Path(handle.path) / "input.txt") == expected
    assert git(target, "cat-file", "-e", blob, check=False).returncode != 0
    (Path(handle.path) / "input.txt").write_text("small prepared result\n")
    manifest, rows = capture(tmp_path, handle)
    assert manifest["patch_size"] == 0 and len(rows) == 1
    assert rows[0]["before"]["sha256"] == expected["sha256"]
    with prepare_file_outputs(rows, target, baseline_sha=handle.baseline_sha,
                              file_baseline=handle.file_baseline) as prepared:
        prepared.apply()
        assert input_path.read_text() == "small prepared result\n"
        prepared.rollback()
    assert stream_artifact_file(input_path) == expected


def test_concurrent_input_edit_is_preserved_before_any_result_apply(tmp_path):
    target = target_tree(tmp_path)
    document(target / "input.docx")
    handle = snapshot(tmp_path, target)
    document(Path(handle.path) / "input.docx", "Prepared child result")
    _, rows = capture(tmp_path, handle)
    document(target / "input.docx", "Concurrent owner edit")
    newer = (target / "input.docx").read_bytes()
    with pytest.raises(ValueError, match="target changed from baseline"):
        with prepare_file_outputs(rows, target, baseline_sha=handle.baseline_sha,
                                  file_baseline=handle.file_baseline):
            pytest.fail("concurrent input must not reach apply")
    assert (target / "input.docx").read_bytes() == newer


def test_child_staging_unchanged_input_does_not_create_a_result(tmp_path):
    target = target_tree(tmp_path)
    document(target / "input.docx")
    handle = snapshot(tmp_path, target)
    git(handle.path, "add", "input.docx")
    manifest, rows = capture(tmp_path, handle)
    assert manifest["status"] == "ready_no_changes" and rows == []


def test_baseline_file_copy_failure_cleans_checkout_ref_and_record(tmp_path, monkeypatch):
    from ouroboros import workspace_file_outputs
    target = target_tree(tmp_path)
    document(target / "input.docx")
    def fail_copy(*args):
        raise OSError("file changed during input copy")
    monkeypatch.setattr(workspace_file_outputs, "copy_snapshot_file_inputs", fail_copy)
    with pytest.raises(OSError, match="changed during input copy"):
        snapshot(tmp_path, target)
    assert find_execution_snapshot("input-snapshot", tmp_path / "data") is None
    assert not git(target, "for-each-ref", "refs/ouroboros/delegated/").stdout
    assert not list((tmp_path / "snapshots").glob("dlg_*"))


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("policy", ["autocrlf", "attributes"])
def test_git_checkout_preserves_working_bytes_and_normal_patch_semantics(tmp_path, newline, policy):
    target = target_tree(tmp_path)
    git(target, "config", "core.autocrlf", "true" if policy == "autocrlf" else "false")
    if policy == "attributes":
        (target / ".gitattributes").write_bytes(b"*.txt text eol=crlf\n")
    original = newline.join([b"original", b"working bytes", b""])
    for name in ("code.txt", "staged.txt", "untracked.txt"):
        (target / name).write_bytes(original)
    git(target, "add", "staged.txt")
    (target / "deleted.txt").write_bytes(b"removed before snapshot\n")
    git(target, "add", "deleted.txt")
    (target / "deleted.txt").unlink()
    before_status = git(target, "status", "--porcelain=v1", "-z").stdout
    before_index = git(target, "ls-files", "--stage", "-z").stdout
    before_head = git(target, "rev-parse", "HEAD").stdout

    handle = snapshot(tmp_path, target)
    execution = Path(handle.path)
    for name in ("code.txt", "staged.txt", "untracked.txt"):
        assert (execution / name).read_bytes() == original
    assert not (execution / "deleted.txt").exists()
    assert git(target, "status", "--porcelain=v1", "-z").stdout == before_status
    assert git(target, "ls-files", "--stage", "-z").stdout == before_index
    assert git(target, "rev-parse", "HEAD").stdout == before_head
    assert all((target / name).read_bytes() == original
               for name in ("code.txt", "staged.txt", "untracked.txt"))
    manifest, rows = capture(tmp_path, handle)
    assert manifest["status"] == "ready_no_changes" and manifest["patch_size"] == 0 and rows == []

    changed = original.replace(b"original", b"child edit")
    (execution / "code.txt").write_bytes(changed)
    manifest, rows = capture(tmp_path, handle)
    assert manifest["status"] == "ready_with_changes" and rows == []
    assert manifest["tracked_changed"] == ["code.txt"]
    patch = tmp_path / "capture" / "workspace.patch"
    git(target, "apply", "--binary", "--check", str(patch))
    git(target, "apply", "--binary", str(patch))
    # Explicit apply retains the target's normal Git checkout representation.
    assert (target / "code.txt").read_bytes() == b"child edit\r\nworking bytes\r\n"
    assert (target / "staged.txt").read_bytes() == original
    assert (target / "untracked.txt").read_bytes() == original
    assert git(target, "ls-files", "--stage", "-z").stdout == before_index
    assert git(target, "rev-parse", "HEAD").stdout == before_head


@pytest.mark.parametrize("failure", ["copy_error", "source_changed"])
def test_regular_input_copy_failure_cleans_snapshot_without_rewriting_target(tmp_path, monkeypatch, failure):
    from ouroboros import artifacts

    target = target_tree(tmp_path)
    before_index = git(target, "ls-files", "--stage", "-z").stdout
    before_head = git(target, "rev-parse", "HEAD").stdout
    before_bytes = (target / "code.txt").read_bytes()
    original = artifacts.copy_artifact_file
    newer = b"concurrent owner edit\n"

    def copy(source, destination, **kwargs):
        if Path(source) == target / "code.txt":
            if failure == "copy_error":
                raise OSError("source changed during copy")
            Path(source).write_bytes(newer)
        return original(source, destination, **kwargs)

    monkeypatch.setattr(artifacts, "copy_artifact_file", copy)
    expected = OSError if failure == "copy_error" else subprocess.CalledProcessError
    with pytest.raises(expected):
        snapshot(tmp_path, target)
    assert (target / "code.txt").read_bytes() == (newer if failure == "source_changed" else before_bytes)
    assert git(target, "ls-files", "--stage", "-z").stdout == before_index
    assert git(target, "rev-parse", "HEAD").stdout == before_head
    assert find_execution_snapshot("input-snapshot", tmp_path / "data") is None
    assert not git(target, "for-each-ref", "refs/ouroboros/delegated/").stdout
    assert not list((tmp_path / "snapshots").glob("dlg_*"))


def test_snapshot_keeps_git_link_entries_out_of_regular_file_copy(tmp_path, monkeypatch):
    from ouroboros import artifacts

    target = target_tree(tmp_path)
    # Git's portable symlink checkout is a regular file containing the target;
    # the baseline mode, not its host file type, still owns that representation.
    git(target, "config", "core.symlinks", "false")
    (target / "link").write_bytes(b"code.txt")
    oid = git(target, "hash-object", "-w", "link").stdout.decode().strip()
    git(target, "update-index", "--add", "--cacheinfo", f"120000,{oid},link")
    commit = git(target, "rev-parse", "HEAD").stdout.decode().strip()
    git(target, "update-index", "--add", "--cacheinfo", f"160000,{commit},nested")
    git(target, "-c", "user.name=Fixture", "-c", "user.email=f@invalid", "commit", "-m", "link entries")
    (target / "nested").mkdir()  # An uninitialized gitlink stays Git-owned.
    before_index = git(target, "ls-files", "--stage", "-z").stdout
    original = artifacts.copy_artifact_file

    def copy(source, destination, **kwargs):
        assert Path(source).name not in {"link", "nested"}
        return original(source, destination, **kwargs)

    monkeypatch.setattr(artifacts, "copy_artifact_file", copy)
    handle = snapshot(tmp_path, target)
    execution = Path(handle.path)
    assert (execution / "link").read_bytes() == b"code.txt"
    assert git(execution, "ls-tree", "HEAD", "link").stdout.startswith(b"120000 blob ")
    assert git(execution, "ls-tree", "HEAD", "nested").stdout.startswith(b"160000 commit ")
    assert git(target, "ls-files", "--stage", "-z").stdout == before_index
    manifest, rows = capture(tmp_path, handle)
    assert manifest["status"] == "ready_no_changes" and rows == []
