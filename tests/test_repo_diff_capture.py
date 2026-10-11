"""#1222 — ONE acceptance repository capture, and the two identities it feeds.

The acceptance packet used to read the repository twice (so the preview and the
exact source could describe two different trees) through ``text=True`` (so a
text-classified non-UTF-8 file raised ``UnicodeDecodeError`` outside the guard
and killed the whole panel), and reported every failure as an EMPTY diff — which
reads as a clean tree.

These pin the replacement: one file-backed bytes capture per round under a real
subprocess timeout, an explicit gap for every nonzero exit/timeout/bounded
cut/undecodable run, readable text retained beside those gaps, a REDACTED text
projection (redacted WHOLE before any cut) for the reviewer, and the EXACT bytes
— including the part past the memory ceiling — streamed privately and never
published.

Every credential literal here is synthetic and assembled at runtime.
"""

from __future__ import annotations

import os
import stat
import subprocess as sp
from types import SimpleNamespace as NS

import pytest

pytestmark = pytest.mark.serial  # Real Git fixtures and capture subprocesses.


def _repo(tmp_path, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    sp.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _commit(repo, message="i"):
    sp.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", message],
           cwd=repo, check=True, capture_output=True)


def _secret():
    # Synthetic, assembled at runtime so this FILE holds no contiguous literal.
    return "sk-" + "or-" + "v1-" + "abcdef1234567890" * 2 + "deadbeef"


def test_non_utf8_tracked_file_keeps_readable_text_and_locates_the_gap(tmp_path):
    """A latin-1 source file is text to git and bytes to us: the readable hunk
    survives, the undecodable run is LOCATED, and nothing raises."""
    from ouroboros.repo_diff_capture import capture_repo_diff, repo_diff_projection

    repo = _repo(tmp_path)
    (repo / "notes.txt").write_bytes(b"first line\nsecond line\n")
    _commit(repo)
    # Valid latin-1, invalid UTF-8 — git still classifies the file as text.
    (repo / "notes.txt").write_bytes(b"first line\nCaf\xe9 na\xefve\nsecond line\n")

    capture = capture_repo_diff(repo)
    assert capture.available is True and capture.spool == ()
    text, decode_gaps = repo_diff_projection(capture)
    assert "notes.txt" in text and "first line" in text   # readable text retained
    assert decode_gaps and decode_gaps[0]["status"] == "undecodable_bytes"
    assert decode_gaps[0]["byte_offset"] > 0 and decode_gaps[0]["bytes"] >= 1
    assert "REPOSITORY DIFF CAPTURE GAPS" in text          # the gap is stated, not implied
    assert b"\xe9" in b"".join(capture.iter_raw())         # the exact bytes are still exact
    assert capture.raw_size == len(b"".join(capture.iter_raw()))


def test_binary_and_mixed_diffs_keep_the_text_hunk_and_name_the_binary_file(tmp_path):
    """A mixed patch must not lose its text hunk to its binary sibling, and the
    binary change is named without claiming its content."""
    from ouroboros.artifacts import materialize_repo_diff_evidence

    repo = _repo(tmp_path)
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 4)
    _commit(repo)
    (repo / "code.py").write_text("x = 2  # changed\n", encoding="utf-8")
    (repo / "blob.bin").write_bytes(bytes(range(255, -1, -1)) * 4)

    text, meta = materialize_repo_diff_evidence(repo, tmp_path, "binary-mixed")
    assert "code.py" in text and "changed" in text   # the text hunk survives
    assert "blob.bin" in text                        # the binary change is not invisible
    assert meta["complete"] is False
    assert meta["capture_disclosure"]["complete"] is False
    assert any(gap["status"] == "binary_content_omitted" for gap in meta["issue"]["gaps"])
    assert "REPOSITORY DIFF CAPTURE GAPS" in text


def _corrupt(tmp_path, name="corrupt"):
    """A Git-REQUIRED root that Git cannot read: a broken gitfile, not a plain folder."""
    folder = tmp_path / name
    folder.mkdir()
    (folder / "a.txt").write_text("hello\n", encoding="utf-8")
    (folder / ".git").write_text("gitdir: /nonexistent/gitdir\n", encoding="utf-8")
    return folder


def test_an_unreadable_repository_is_a_stated_gap_not_a_clean_tree(tmp_path):
    """`git diff HEAD` failing is UNKNOWN. Reporting "" would tell the reviewer
    the tree is clean — the exact false-green #1222 is about. (A folder that is
    proven plain is a different, stated fact: see the plain-folder tests.)"""
    from ouroboros.artifacts import materialize_repo_diff_evidence
    from ouroboros.repo_diff_capture import capture_repo_diff, proven_plain_folder

    not_a_repo = _corrupt(tmp_path)
    assert proven_plain_folder(not_a_repo) is False

    capture = capture_repo_diff(not_a_repo)
    assert capture.applicable is True and capture.available is False
    assert any(gap["status"] in {"git_exit_nonzero", "git_unavailable"} for gap in capture.gaps)

    text, meta = materialize_repo_diff_evidence(not_a_repo, tmp_path, "task-1")
    assert meta["complete"] is False
    assert meta["issue"]["status"] == "source_unavailable"
    assert meta["issue"]["reason"] == "repo_diff_capture_unavailable"
    assert "REPOSITORY DIFF CAPTURE GAPS" in text
    assert text.strip() != ""


def test_a_missing_git_binary_is_a_stated_gap_under_the_real_subprocess_path(tmp_path, monkeypatch):
    from ouroboros.repo_diff_capture import capture_repo_diff

    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    capture = capture_repo_diff(repo)
    assert capture.available is False
    assert {gap["status"] for gap in capture.gaps} == {"git_unavailable"}


def test_collect_turn_diff_reports_the_capture_gap_as_a_typed_partial_source(tmp_path):
    """The packet-level view of the same fact: an unavailable capture rides the
    evidence's partial-source list instead of vanishing."""
    from ouroboros.review_evidence import collect_turn_diff

    state: dict = {}
    text = collect_turn_diff(NS(repo_dir=_corrupt(tmp_path)), capture_meta=state)
    assert state["issue"]["reason"] == "repo_diff_capture_unavailable"
    assert state["capture_disclosure"]["available"] is False
    assert state["capture_disclosure"]["complete"] is False
    assert "REPOSITORY DIFF CAPTURE GAPS" in text


def test_a_bounded_capture_keeps_the_bytes_it_could_not_hold_in_memory(tmp_path):
    """Bounded MEMORY is not permission to shorten the truth: the section past
    the ceiling stays on the private spool, the cut is disclosed with the real
    total, and the retention streams the WHOLE source."""
    from ouroboros.observability import posix_private_modes_supported
    from ouroboros.repo_diff_capture import (
        capture_repo_diff, read_private_capture, retain_private_capture,
    )

    repo = _repo(tmp_path)
    (repo / "big.py").write_text("x = 0\n", encoding="utf-8")
    _commit(repo)
    (repo / "big.py").write_text("\n".join(f"v{i} = {i}" for i in range(40000)), encoding="utf-8")

    capture = capture_repo_diff(repo, limit=4096)
    truncation = [gap for gap in capture.gaps if gap["status"] == "capture_bytes_truncated"]
    assert truncation, capture.gaps
    assert truncation[0]["captured_bytes"] <= 4096 < truncation[0]["total_bytes"]
    assert capture.available is True                 # observed the tree; partial, not unavailable
    assert len(capture.section("tracked")) <= 4096
    assert capture.raw_size >= truncation[0]["total_bytes"]
    spooled = dict(capture.spool)["tracked"]
    assert os.path.isfile(spooled)
    # Windows stat modes do not describe ACL privacy; retain the POSIX check
    # where it is meaningful without skipping byte retention on Windows.
    if posix_private_modes_supported():
        assert (os.stat(spooled).st_mode & 0o077) == 0

    private = retain_private_capture(tmp_path, capture)
    assert private["access"] == "host_private" and private["raw_bytes"] == capture.raw_size
    assert not os.path.exists(spooled)               # released once retained
    raw = read_private_capture(tmp_path, private)
    assert len(raw) == capture.raw_size and raw.count(b"v39999 = 39999") == 1
    if posix_private_modes_supported():
        assert (os.stat(private["blob_ref"]["path"]).st_mode & 0o077) == 0


def test_a_retention_that_cannot_happen_is_disclosed_never_implied(tmp_path, monkeypatch):
    import ouroboros.repo_diff_capture as cap
    from ouroboros.artifacts import materialize_repo_diff_evidence
    from ouroboros.repo_diff_capture import capture_repo_diff

    repo = _repo(tmp_path)
    (repo / "big.py").write_text("x = 0\n", encoding="utf-8")
    _commit(repo)
    (repo / "big.py").write_text("\n".join(f"v{i} = {i}" for i in range(6000)), encoding="utf-8")
    monkeypatch.setattr(cap, "write_blob_stream",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    capture = capture_repo_diff(repo, limit=2048)
    spooled = dict(capture.spool)["tracked"]
    _text, meta = materialize_repo_diff_evidence(repo, tmp_path, "task-1", capture=capture)
    assert meta["capture_disclosure"]["raw_retained"] is False
    assert meta["capture_disclosure"]["raw_retention"]["status"] == "unavailable"
    assert "private_source_ref" not in meta
    assert not os.path.exists(spooled)               # the spool never outlives its use


def test_the_raw_bytes_stay_private_and_the_reviewer_gets_the_masked_projection(tmp_path):
    """Two identities, never interchangeable: the exact source is retained in the
    private observability CAS, and only the REDACTED text is published."""
    import json

    from ouroboros.artifacts import materialize_repo_diff_evidence
    from ouroboros.repo_diff_capture import capture_repo_diff, read_private_capture

    repo = _repo(tmp_path)
    (repo / "conf.py").write_text('API_KEY = "placeholder"\n', encoding="utf-8")
    _commit(repo)
    secret = _secret()
    (repo / "conf.py").write_text(f'API_KEY = "{secret}"\n', encoding="utf-8")

    capture = capture_repo_diff(repo)
    published, meta = materialize_repo_diff_evidence(repo, tmp_path, "task-1", capture=capture)
    assert secret not in published and "REDACTED" in published
    assert secret not in json.dumps(meta.get("capture_disclosure") or {})
    private = meta["private_source_ref"]
    assert private["access"] == "host_private"
    assert meta["capture_disclosure"]["raw_retained"] is True
    assert read_private_capture(tmp_path, private).decode("utf-8", "replace").count(secret) == 1


def test_redaction_happens_before_any_cut_so_a_secret_straddling_the_bound_never_leaks(tmp_path):
    """A presentation bound applied BEFORE redaction would split the credential
    and hand the redactor an unrecognizable fragment. The projection redacts
    each section whole first, so no bound can expose part of it."""
    from ouroboros.repo_diff_capture import capture_repo_diff, repo_diff_projection

    repo = _repo(tmp_path)
    (repo / "conf.py").write_text("PREFIX = 1\n", encoding="utf-8")
    _commit(repo)
    secret = _secret()
    (repo / "conf.py").write_text(f'PREFIX = 1\nAPI_KEY = "{secret}"\n', encoding="utf-8")
    capture = capture_repo_diff(repo)
    full, _ = repo_diff_projection(capture)
    assert "REDACTED" in full
    unredacted_offset = capture.section("tracked").decode("utf-8").index(secret)
    for limit in range(unredacted_offset - 8, unredacted_offset + len(secret) + 8, 7):
        text, _ = repo_diff_projection(capture, section_limits={"tracked": limit})
        assert secret not in text
        assert secret[:12] not in text and secret[-12:] not in text, limit


def _evidence_ctx(repo, tmp_path):
    """The real tool context the host builder reads, bound to this repo."""
    from ouroboros.tools.registry import ToolRegistry

    registry = ToolRegistry(repo_dir=repo, drive_root=tmp_path)
    registry._ctx.task_id = "task-1"
    registry._ctx.root_task_id = "task-1"
    registry._ctx.task_metadata = {"root_task_id": "task-1"}
    registry._ctx.task_contract = {}
    return registry._ctx


def test_the_evidence_packet_publishes_the_digest_but_never_the_raw_source_ref(tmp_path):
    """The packet may say WHAT the source was (digest, size, gaps); it may not
    carry a handle a reviewer, an export or a download could resolve."""
    import json

    from ouroboros.review_evidence import build_task_acceptance_evidence

    repo = _repo(tmp_path)
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo)
    (repo / "code.py").write_bytes(b"x = 2\n# caf\xe9\n")

    evidence = build_task_acceptance_evidence(
        _evidence_ctx(repo, tmp_path), drive_root=tmp_path, task_id="task-1", agent_evidence={},
    )
    assert evidence["repo_diff_capture"]["raw_sha256"]
    assert evidence["repo_diff_capture"]["projection"] == "redacted_text"
    serialized = json.dumps(evidence, ensure_ascii=False, default=str)
    assert "private_source_ref" not in serialized
    assert "observability_blob" not in serialized


def test_one_round_reads_the_repository_exactly_once_and_leaves_no_spool(tmp_path, monkeypatch):
    """The preview and the exact source must describe the SAME tree: two
    independent reads could straddle a concurrent write."""
    from ouroboros import repo_diff_capture as capture_mod
    from ouroboros.review_evidence import build_task_acceptance_evidence

    repo = _repo(tmp_path)
    (repo / "big.py").write_text("x = 0\n", encoding="utf-8")
    _commit(repo)
    # Large enough that the bounded preview is cut and the exact path is owed.
    (repo / "big.py").write_text("\n".join(f"v{i} = {i}" for i in range(6000)), encoding="utf-8")

    captures = []
    real = capture_mod.capture_repo_diff

    def _counted(repo_dir, **kwargs):
        capture = real(repo_dir, limit=2048, **kwargs)   # force a spool so its release is observable
        captures.append(capture)
        return capture

    monkeypatch.setattr(capture_mod, "capture_repo_diff", _counted)
    evidence = build_task_acceptance_evidence(
        _evidence_ctx(repo, tmp_path), drive_root=tmp_path, task_id="task-1", agent_evidence={},
    )
    assert len(captures) == 1, captures
    assert "Section withheld" in evidence["repo_diff"]
    assert evidence["repo_diff_capture"]["complete"] is False
    assert captures[0].spool and not any(os.path.exists(path) for _n, path in captures[0].spool)


def test_decode_gaps_stay_bounded_on_a_pathologically_binary_source():
    """Many undecodable runs must not cost quadratic work or unbounded rows."""
    from ouroboros.repo_diff_capture import _MAX_DECODE_GAPS, decode_capture_text

    data = b"ok\n" + b"\xff\xfe" * 5000 + b"\ntail\n"
    text, gaps = decode_capture_text(data)
    assert "ok" in text and "tail" in text
    assert len(gaps) <= _MAX_DECODE_GAPS + 1
    assert gaps[-1]["located"] is False   # the remainder is summarized, not dropped


@pytest.mark.parametrize("include_commit", [False, True])
def test_capture_sections_match_the_historical_evidence_layout(tmp_path, include_commit):
    """Existing readers find untracked files and this turn's commit by these
    exact header lines; the capture owns them so both projections agree."""
    from ouroboros.repo_diff_capture import capture_repo_diff, repo_diff_projection_text

    repo = _repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo, "base")
    (repo / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _commit(repo, "feature")
    (repo / "a.py").write_text("x = 2  # tweaked\n", encoding="utf-8")
    (repo / "scratch_test.py").write_text("def test_x(): pass\n", encoding="utf-8")

    text = repo_diff_projection_text(
        capture_repo_diff(repo, include_recent_commit=include_commit),
    )
    assert "tweaked" in text
    assert "Untracked working-tree files" in text and "scratch_test.py" in text
    assert ("committed this turn" in text) is include_commit
    assert ("feature.py" in text) is include_commit


# ── the SOURCE identity the local preparation binds to ─────────────────────


def test_repo_source_identity_is_stable_over_an_unchanged_tree_and_moves_with_its_bytes(tmp_path):
    from ouroboros.repo_diff_capture import repo_source_identity

    repo = _repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo)
    (repo / "a.py").write_bytes(b"x = 2\n# caf\xe9\n")
    first = repo_source_identity(repo)
    assert first == repo_source_identity(repo)
    (repo / "a.py").write_text("x = 2\n# cafe\n", encoding="utf-8")   # same size, new bytes
    assert repo_source_identity(repo) != first
    with pytest.raises(RuntimeError):
        repo_source_identity(_corrupt(tmp_path))     # unreadable raises; the caller says "unknown"


def test_repo_source_identity_stays_bounded_past_its_byte_budget(tmp_path):
    from ouroboros.repo_diff_capture import repo_source_identity

    repo = _repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo)
    (repo / "huge.bin").write_bytes(b"\x00" * 3000)
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="identity incomplete"):
        repo_source_identity(repo, max_bytes=1000)
    (repo / "huge.bin").write_bytes(b"\x00" * 3001)
    with pytest.raises(RuntimeError, match="identity incomplete"):
        repo_source_identity(repo, max_bytes=1000)


def test_staging_and_index_only_edits_do_not_change_preparation_material(tmp_path):
    """Only the source Git diff HEAD would show can reopen preparation."""
    from ouroboros.repo_diff_capture import repo_source_identity

    repo = _repo(tmp_path)
    source = repo / "a.py"
    source.write_text("x = 1\n")
    _commit(repo)
    clean = repo_source_identity(repo)
    source.write_text("x = 2\n")
    changed = repo_source_identity(repo)
    assert changed != clean
    sp.run(["git", "add", "a.py"], cwd=repo, check=True, capture_output=True)
    assert repo_source_identity(repo) == changed
    # Index still holds x=2, but the working-tree-vs-HEAD source is clean.
    source.write_text("x = 1\n")
    assert repo_source_identity(repo) == clean


def test_head_read_failure_is_unknown_instead_of_an_unborn_identity(tmp_path, monkeypatch):
    from ouroboros import repo_diff_capture as cap

    repo = _repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n")
    _commit(repo)
    original = cap._run_git_to_file

    def fail_head(root, args, **kwargs):
        path, gap = original(root, args, **kwargs)
        if args[0] == "rev-parse":
            gap = {"status": "git_timeout"}
        return path, gap

    monkeypatch.setattr(cap, "_run_git_to_file", fail_head)
    with pytest.raises(RuntimeError, match="HEAD identity unavailable"):
        cap.repo_source_identity(repo)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_unborn_repository_uses_empty_tree_for_capture_and_identity(tmp_path, object_format):
    from ouroboros.repo_diff_capture import capture_repo_diff, repo_source_identity

    repo = tmp_path / object_format
    repo.mkdir()
    sp.run(["git", "init", f"--object-format={object_format}"], cwd=repo, check=True, capture_output=True)
    empty = capture_repo_diff(repo, include_recent_commit=True)
    assert empty.complete and empty.raw_size == 0
    initial = repo_source_identity(repo)
    source = repo / "new.txt"
    source.write_bytes(b"uncommitted caf\xe9\n")
    changed = repo_source_identity(repo)
    assert changed != initial
    sp.run(["git", "add", "new.txt"], cwd=repo, check=True, capture_output=True)
    assert repo_source_identity(repo) == changed  # Index status is still not material.
    source.write_bytes(b"working tree caf\xe9\n")
    capture = capture_repo_diff(repo, include_recent_commit=True)
    try:
        assert capture.complete
        assert b"+working tree caf\xe9" in capture.section("tracked")
        assert not capture.section("commit")  # An unborn repository has no recent commit.
        assert repo_source_identity(repo) != changed
    finally:
        capture.release()


@pytest.mark.parametrize("failure", ["corrupt_ref", "missing_object", "detached_missing", "head_read_error"])
def test_git_failure_is_never_guessed_to_be_unborn(tmp_path, monkeypatch, failure):
    from ouroboros import repo_diff_capture as cap

    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("baseline\n")
    _commit(repo)
    branch = sp.check_output(["git", "symbolic-ref", "HEAD"], cwd=repo).decode().strip()
    if failure == "corrupt_ref":
        (repo / ".git" / branch).write_text("not an object id\n")
    elif failure == "missing_object":
        head = sp.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
        obj = repo / ".git" / "objects" / head[:2] / head[2:]
        # Git's loose objects are read-only; Windows requires clearing that
        # attribute before this fixture can deliberately remove its own object.
        obj.chmod(obj.stat().st_mode | stat.S_IWRITE)
        obj.unlink()
        assert not obj.exists()
    elif failure == "detached_missing":
        (repo / ".git" / "HEAD").write_text("f" * 40 + "\n")
    else:
        original = cap._run_git_to_file

        def fail_head(root, args, **kwargs):
            path, gap = original(root, args, **kwargs)
            return path, ({"status": "git_exit_nonzero", "returncode": 128}
                          if args[0] == "rev-parse" else gap)

        monkeypatch.setattr(cap, "_run_git_to_file", fail_head)
    capture = cap.capture_repo_diff(repo)
    try:
        assert not capture.available and capture.gaps
        with pytest.raises(RuntimeError, match="HEAD identity unavailable"):
            cap.repo_source_identity(repo)
    finally:
        capture.release()


def test_memory_cut_inside_secret_withholds_section_and_retains_full_raw(tmp_path):
    from ouroboros.repo_diff_capture import capture_repo_diff, repo_diff_projection, retain_private_capture, read_private_capture

    repo = _repo(tmp_path)
    (repo / 'a.py').write_text('x = 1\n')
    _commit(repo)
    secret = _secret()
    (repo / 'a.py').write_text('x = 2\n' + secret + '\n')
    full = capture_repo_diff(repo)
    offset = full.section('tracked').index(secret.encode())
    capture = capture_repo_diff(repo, limit=offset + 12)
    text, _ = repo_diff_projection(capture)
    assert secret[:12] not in text
    assert 'Section withheld' in text
    retained = retain_private_capture(tmp_path, capture)
    assert secret.encode() in read_private_capture(tmp_path, retained)


@pytest.mark.parametrize("failure", ["exit", "timeout", "unavailable"])
@pytest.mark.parametrize("secret_offset", [490, 2040])
def test_failed_git_output_never_publishes_diagnostic_or_stdout_fragments(tmp_path, monkeypatch, failure, secret_offset):
    import json
    from ouroboros import repo_diff_capture as cap
    from ouroboros.artifacts import materialize_repo_diff_evidence

    secret = _secret()
    prefix = secret[:18]
    diagnostic = "x" * secret_offset + secret + "\n-----BEGIN PRIVATE KEY-----\nprivate-fragment"

    def failed_run(argv, **kwargs):
        kwargs["stdout"].write(prefix.encode())  # interrupted in the middle of a credential
        kwargs["stderr"].write(diagnostic.encode())
        if failure == "timeout":
            raise sp.TimeoutExpired(argv, 1, output=prefix, stderr=diagnostic)
        if failure == "unavailable":
            raise OSError(diagnostic)
        return NS(returncode=128)

    (tmp_path / ".git").mkdir()  # Git-required fixture, not a proven plain folder.
    monkeypatch.setattr(cap.subprocess, "run", failed_run)
    capture = cap.capture_repo_diff(tmp_path)
    text, meta = materialize_repo_diff_evidence(tmp_path, tmp_path, "diagnostics", capture=capture)
    published = text + json.dumps(meta["capture_disclosure"])
    assert prefix not in published and secret[-12:] not in published
    assert "private-fragment" not in published
    assert "Section withheld" in text and "CAPTURE GAPS" in text
    assert not meta["complete"] and not meta["capture_disclosure"]["available"]
    assert prefix.encode() in cap.read_private_capture(tmp_path, meta["private_source_ref"])


def test_gap_disclosure_is_redacted_before_presentation(tmp_path):
    import json
    from ouroboros.repo_diff_capture import RepoDiffCapture, capture_disclosure, repo_diff_projection

    capture = RepoDiffCapture(gaps=({"section": "tracked", "status": "git_exit_nonzero",
                                     "detail": _secret()},))
    text, _ = repo_diff_projection(capture)
    assert _secret() not in text + json.dumps(capture_disclosure(capture))


def test_small_complete_capture_is_retained_privately_once(tmp_path, monkeypatch):
    from ouroboros import repo_diff_capture as cap
    from ouroboros.review_evidence import collect_turn_diff

    repo = _repo(tmp_path)
    (repo / "small.txt").write_text("old\n")
    _commit(repo)
    (repo / "small.txt").write_text(_secret() + "\n")
    writes = []
    original = cap.write_blob_stream

    def retain(*args, **kwargs):
        ref = original(*args, **kwargs)
        writes.append(ref)
        return ref

    monkeypatch.setattr(cap, "write_blob_stream", retain)
    meta = {}
    text = collect_turn_diff(NS(repo_dir=repo, drive_root=tmp_path), capture_meta=meta)
    assert meta["exact_required"] is False
    assert meta["capture_disclosure"]["raw_retained"] is True and len(writes) == 1
    assert _secret() not in text
    assert _secret().encode() in cap.read_private_capture(tmp_path, {
        "blob_ref": writes[0], "raw_sha256": meta["capture_disclosure"]["raw_sha256"]})
    assert not any(os.path.exists(path) for _, path in meta["capture"].spool)


@pytest.mark.parametrize("consumer", ["preview", "exact"])
def test_projection_exception_releases_spool(tmp_path, monkeypatch, consumer):
    from ouroboros import repo_diff_capture as cap
    from ouroboros.artifacts import materialize_repo_diff_evidence
    from ouroboros.review_evidence import collect_turn_diff

    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("old\n")
    _commit(repo)
    (repo / "a.txt").write_text("new\n")
    capture = cap.capture_repo_diff(repo, limit=1)
    assert capture.spool
    monkeypatch.setattr(cap, "capture_repo_diff", lambda *_a, **_k: capture)
    monkeypatch.setattr(cap, "repo_diff_projection",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("projection unavailable")))
    with pytest.raises(ValueError, match="projection unavailable"):
        if consumer == "preview":
            collect_turn_diff(NS(repo_dir=repo, drive_root=tmp_path), capture_meta={})
        else:
            materialize_repo_diff_evidence(repo, tmp_path, "cleanup", capture=capture)
    assert not any(os.path.exists(path) for _, path in capture.spool)


def test_capture_without_exact_consumer_retains_then_releases_large_source(tmp_path, monkeypatch):
    from ouroboros import repo_diff_capture as cap
    from ouroboros.review_evidence import collect_turn_diff

    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("old\n")
    _commit(repo)
    (repo / "a.txt").write_text("new\n")
    capture = cap.capture_repo_diff(repo, limit=1)
    calls = []
    original = cap.retain_private_capture
    monkeypatch.setattr(cap, "capture_repo_diff", lambda *_a, **_k: capture)

    def retained(*args, **kwargs):
        ref = original(*args, **kwargs)
        calls.append(ref)
        return ref

    monkeypatch.setattr(cap, "retain_private_capture", retained)
    collect_turn_diff(NS(repo_dir=repo, drive_root=tmp_path))
    assert len(calls) == 1 and calls[0]["blob_ref"]
    assert not any(os.path.exists(path) for _, path in capture.spool)


# ── diff-marked assignments and private-key blocks (Astra findings, #1222 follow-up) ──


def _opaque(tag):
    # Synthetic and WITHOUT a provider prefix: only the assignment rule can catch it.
    return f"opaque-{tag}-" + "0123456789abcdef" * 2


def _pem():
    # Synthetic key material, assembled at runtime so this FILE holds no contiguous block.
    body = "\n".join(["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC"] * 3)
    return "-----BEGIN " + "PRIVATE KEY-----\n" + body + "\n-----END " + "PRIVATE KEY-----\n"


def test_diff_marked_assignments_are_redacted_while_the_raw_bytes_keep_both_values(tmp_path):
    """A changed line starts with its diff marker, not with the key: ``+API_KEY = ...``
    and ``-API_KEY = ...`` used to pass the assignment rule untouched."""
    from ouroboros.repo_diff_capture import (
        capture_repo_diff, read_private_capture, repo_diff_projection, retain_private_capture,
    )

    repo = _repo(tmp_path)
    old, new = _opaque("old"), _opaque("new")
    (repo / "conf.py").write_text(f'API_KEY = "{old}"\n', encoding="utf-8")
    _commit(repo)
    (repo / "conf.py").write_text(f'API_KEY = "{new}"\n', encoding="utf-8")
    capture = capture_repo_diff(repo)
    raw = capture.section("tracked")
    assert f'-API_KEY = "{old}"'.encode() in raw and f'+API_KEY = "{new}"'.encode() in raw
    text, _ = repo_diff_projection(capture)
    assert old not in text and new not in text
    assert '-API_KEY = "***REDACTED***"' in text and '+API_KEY = "***REDACTED***"' in text
    assert "+++ b/conf.py" in text and "--- a/conf.py" in text          # file headers untouched
    private = read_private_capture(tmp_path, retain_private_capture(tmp_path, capture))
    assert old.encode() in private and new.encode() in private        # exact bytes stay private


def test_an_unmarked_context_line_and_a_marked_line_redact_alike(tmp_path):
    from ouroboros.repo_diff_capture import capture_repo_diff, repo_diff_projection

    repo = _repo(tmp_path)
    kept, added = _opaque("kept"), _opaque("added")
    (repo / "conf.py").write_text(f'TOKEN = "{kept}"\nx = 1\n', encoding="utf-8")
    _commit(repo)
    (repo / "conf.py").write_text(f'TOKEN = "{kept}"\nx = 1\nSECRET = "{added}"\n', encoding="utf-8")
    text, _ = repo_diff_projection(capture_repo_diff(repo))
    assert kept not in text and added not in text
    assert text.count("***REDACTED***") == 2


def test_a_private_key_added_in_a_diff_is_masked_whole_before_any_cut(tmp_path):
    from ouroboros.repo_diff_capture import (
        capture_repo_diff, read_private_capture, repo_diff_projection, retain_private_capture,
    )

    repo = _repo(tmp_path)
    (repo / "conf.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo)
    pem = _pem()
    (repo / "conf.py").write_text("x = 1\n" + pem, encoding="utf-8")
    capture = capture_repo_diff(repo)
    material = pem.splitlines()[1]
    assert material.encode() in capture.section("tracked")
    text, _ = repo_diff_projection(capture)
    assert material not in text and "PRIVATE KEY" not in text and "***REDACTED***" in text
    offset = capture.section("tracked").decode("utf-8").index(material)
    for limit in range(offset - 8, offset + len(material) + 8, 7):
        cut, _ = repo_diff_projection(capture, section_limits={"tracked": limit})
        assert material not in cut and material[:12] not in cut and "PRIVATE KEY" not in cut, limit
    private = read_private_capture(tmp_path, retain_private_capture(tmp_path, capture))
    assert all(line.encode() in private for line in pem.splitlines())


# ── private temp custody on construction failure ──


def test_a_failed_second_spool_file_closes_and_removes_the_first(tmp_path, monkeypatch):
    import time
    from ouroboros import repo_diff_capture as cap

    created = []
    real = cap.tempfile.mkstemp

    def one_then_fail(*args, **kwargs):
        if created:
            raise OSError("no more temp files")
        fd, path = real(*args, **kwargs)
        created.append((fd, path))
        return fd, path

    monkeypatch.setattr(cap.tempfile, "mkstemp", one_then_fail)
    with pytest.raises(OSError, match="no more temp files"):
        cap._run_git_to_file(tmp_path, ["status"], deadline=time.monotonic() + 5)
    fd, path = created[0]
    assert not os.path.exists(path)
    with pytest.raises(OSError):
        os.fstat(fd)  # the descriptor was closed, not leaked


def test_a_later_section_failure_releases_the_spool_of_an_earlier_section(tmp_path, monkeypatch):
    from ouroboros import repo_diff_capture as cap

    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("old\n")
    _commit(repo)
    (repo / "a.txt").write_text("new " * 64 + "\n")
    original = cap._run_git_to_file
    paths = []

    def fail_untracked(root, args, **kwargs):
        if args[0] == "ls-files":
            raise OSError("temp files exhausted")
        path, gap = original(root, args, **kwargs)
        paths.append(path)
        return path, gap

    monkeypatch.setattr(cap, "_run_git_to_file", fail_untracked)
    with pytest.raises(OSError, match="temp files exhausted"):
        cap.capture_repo_diff(repo, limit=1)  # the tracked section exceeds the ceiling and is spooled
    assert paths and not any(os.path.exists(path) for path in paths)


# ── a proven plain folder: no baseline (never a clean tree), never a parent repository ──


def _plain(root, name="plain"):
    folder = root / name
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"hello caf\xe9\n")
    (folder / "sub").mkdir()
    (folder / "sub" / "b.py").write_text("x = 1\n", encoding="utf-8")
    return folder


def test_a_proven_plain_folder_is_not_applicable_complete_and_never_calls_git(tmp_path, monkeypatch):
    """`.git` and `HEAD` both absent by lstat: no baseline exists, so the capture is
    applicable=False and complete — a stated non-applicability, never an empty diff,
    never a gap and never a `source_unavailable` partial — and no Git process runs."""
    from ouroboros import repo_diff_capture as cap
    from ouroboros.artifacts import materialize_repo_diff_evidence
    from ouroboros.review_evidence import collect_turn_diff

    plain = _plain(tmp_path)
    monkeypatch.setattr(cap, "_run_git_to_file", lambda *_a, **_k: pytest.fail("git was run on a plain folder"))
    assert cap.proven_plain_folder(plain) is True
    capture = cap.capture_repo_diff(plain, include_recent_commit=True)
    assert capture.applicable is False and capture.available and capture.complete and not capture.gaps
    text, gaps = cap.repo_diff_projection(capture)
    assert "NOT APPLICABLE" in text and "NOT a clean-tree claim" in text and gaps == []
    disclosure = cap.capture_disclosure(capture)
    assert disclosure["applicable"] is False and disclosure["complete"] is True and disclosure["gaps"] == []
    state: dict = {}
    preview = collect_turn_diff(NS(repo_dir=plain), capture_meta=state)
    assert "NOT APPLICABLE" in preview and "issue" not in state and state["exact_required"] is False
    assert state["capture_disclosure"]["applicable"] is False
    exact, meta = materialize_repo_diff_evidence(plain, tmp_path, "task-1")
    assert meta["complete"] is True and "issue" not in meta and "NOT APPLICABLE" in exact
    assert meta["capture_disclosure"]["applicable"] is False


def test_a_plain_folder_packet_refuses_nothing_and_states_no_baseline(tmp_path):
    """The full evidence over a plain workspace carries no `source_unavailable`
    partial, so the zero-physical refusal is empty — acceptance may run — while the
    packet states that no baseline exists rather than presenting a clean tree."""
    from ouroboros.review_dispatch import task_acceptance_zero_physical_refusal
    from ouroboros.review_evidence import build_task_acceptance_evidence

    plain = _plain(tmp_path)
    evidence = build_task_acceptance_evidence(
        _evidence_ctx(plain, tmp_path), drive_root=tmp_path, task_id="task-1", agent_evidence={},
    )
    assert evidence["repo_diff_capture"]["applicable"] is False
    assert evidence["repo_diff_capture"]["complete"] is True
    assert "NOT APPLICABLE" in evidence["repo_diff"]
    assert not [row for row in evidence.get("__unresolved_partial_artifacts__") or []
                if row.get("status") == "source_unavailable"]
    assert task_acceptance_zero_physical_refusal(evidence) == {}


def test_a_plain_folder_inside_a_repository_never_leaks_its_parent(tmp_path, monkeypatch):
    """The selected root alone decides: a plain subfolder of a checkout has no
    `.git`/`HEAD` of its own, so it is captured and identified as a plain folder;
    the parent repository is neither discovered by Git nor part of the identity."""
    from ouroboros import repo_diff_capture as cap

    parent = _repo(tmp_path)
    (parent / "outside.txt").write_text("parent\n", encoding="utf-8")
    _commit(parent)
    plain = _plain(parent)
    monkeypatch.setattr(cap, "_run_git_to_file", lambda *_a, **_k: pytest.fail("git was run on a plain folder"))
    assert cap.capture_repo_diff(plain).applicable is False
    first = cap.repo_source_identity(plain)
    (parent / "outside.txt").write_text("parent moved\n", encoding="utf-8")   # not this folder's material
    assert cap.repo_source_identity(plain) == first
    (plain / "a.txt").write_bytes(b"hello cafe\n")                              # same size, new bytes
    assert cap.repo_source_identity(plain) != first


def test_a_plain_folder_identity_is_content_bounded_and_symlink_safe(tmp_path):
    """Deterministic over the same bytes; moved by a same-size edit, an added or a
    removed file; a symlink (a directory one included) is its target and is never
    traversed; a nested repository's `.git` internals are skipped; a special file
    or an inventory past its bounds RAISES (unknown), never a size/mtime proxy."""
    from ouroboros.repo_diff_capture import repo_source_identity

    plain = _plain(tmp_path)
    first = repo_source_identity(plain)
    assert first == repo_source_identity(plain)
    (plain / "sub" / "b.py").write_text("x = 2\n", encoding="utf-8")           # same size, new bytes
    edited = repo_source_identity(plain)
    assert edited != first
    (plain / "c.txt").write_text("new\n", encoding="utf-8")
    added = repo_source_identity(plain)
    assert added != edited
    (plain / "c.txt").unlink()
    assert repo_source_identity(plain) == edited
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "t.txt").write_text("t\n", encoding="utf-8")
    os.symlink(target, plain / "link")                                          # a directory symlink
    linked = repo_source_identity(plain)
    assert linked != edited
    (target / "t.txt").write_text("changed behind the link\n", encoding="utf-8")
    assert repo_source_identity(plain) == linked                                # never traversed
    nested = _repo(plain, "nested")
    (nested / "n.txt").write_text("n\n", encoding="utf-8")
    with_nested = repo_source_identity(plain)
    assert with_nested != linked
    (nested / ".git" / "touched").write_text("internals moved\n", encoding="utf-8")
    assert repo_source_identity(plain) == with_nested                           # `.git` internals are not content
    with pytest.raises(RuntimeError, match="identity incomplete"):
        repo_source_identity(plain, max_files=2)
    with pytest.raises(RuntimeError, match="identity incomplete"):
        repo_source_identity(plain, max_bytes=4)
    if hasattr(os, "mkfifo"):
        os.mkfifo(plain / "pipe")
        with pytest.raises(RuntimeError, match="identity incomplete"):
            repo_source_identity(plain)


def test_any_git_marker_keeps_a_folder_git_required(tmp_path):
    """Any `.git` or `HEAD` entry — an empty directory, a broken gitfile, a bare
    HEAD — is NOT a plain folder: the capture stays unavailable with its typed gap
    and the identity raises (unknown); so do a missing root and a file."""
    from ouroboros.repo_diff_capture import capture_repo_diff, proven_plain_folder, repo_source_identity

    for name, marker, content in (("gitfile", ".git", "gitdir: /nonexistent\n"),
                                  ("gitdir", ".git", None), ("bare", "HEAD", "ref: refs/heads/main\n")):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "a.txt").write_text("hello\n", encoding="utf-8")
        if content is None:
            (folder / marker).mkdir()
        else:
            (folder / marker).write_text(content, encoding="utf-8")
        assert proven_plain_folder(folder) is False, name
        capture = capture_repo_diff(folder)
        assert capture.applicable is True and capture.available is False, name
        with pytest.raises(RuntimeError):
            repo_source_identity(folder)
    assert proven_plain_folder(tmp_path / "missing") is False
    assert proven_plain_folder(tmp_path / "gitfile" / "a.txt") is False


def test_git_retargeting_environment_never_captures_a_foreign_repository(tmp_path, monkeypatch):
    """`GIT_DIR`/`GIT_WORK_TREE` in the host environment would make git read a
    FOREIGN repository from the selected root (or turn a plain folder into one): the
    capture scrubs them, so the selected root alone is captured and identified."""
    from ouroboros.repo_diff_capture import capture_repo_diff, repo_source_identity

    selected = _repo(tmp_path, "selected")
    (selected / "mine.txt").write_text("mine\n", encoding="utf-8")
    _commit(selected)
    (selected / "mine.txt").write_text("mine changed\n", encoding="utf-8")
    identity = repo_source_identity(selected)
    foreign = _repo(tmp_path, "foreign")
    (foreign / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _commit(foreign)
    (foreign / "theirs.txt").write_text("theirs changed\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    capture = capture_repo_diff(selected)
    try:
        assert capture.complete
        assert b"+mine changed" in capture.section("tracked") and b"theirs" not in capture.section("tracked")
    finally:
        capture.release()
    assert repo_source_identity(selected) == identity
    assert capture_repo_diff(_plain(tmp_path)).applicable is False   # still plain, not the foreign tree
