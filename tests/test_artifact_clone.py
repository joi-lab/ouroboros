"""``artifacts.copy_artifact_file`` clones where the filesystem shares blocks (APFS here), streams a copy
elsewhere, never hard-links, and keeps its identity verification either way."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

from ouroboros import artifact_storage, artifacts


def _measured(path):
    raw = path.read_bytes()
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def test_copy_clones_on_this_filesystem_and_the_copy_is_its_own_inode(tmp_path, monkeypatch):
    source, destination = tmp_path / "result.bin", tmp_path / "artifacts" / "result.bin"
    source.write_bytes(os.urandom(1 << 20) * 24)  # 24 MiB of incompressible bytes
    seen, original = [], artifacts.clone_file

    def observed(src, dst):
        outcome = original(src, dst)
        seen.append(outcome)
        return outcome

    monkeypatch.setattr(artifacts, "clone_file", observed)  # the name the copy seam binds
    free_before = shutil.disk_usage(tmp_path).free
    measured = artifacts.copy_artifact_file(source, destination, expected=_measured(source))
    free_after = shutil.disk_usage(tmp_path).free
    print(json.dumps({"case": "clone", "cloned": seen, "free_delta_bytes": free_before - free_after,
                      "size": measured["size"]}), flush=True)
    assert measured == _measured(source) == _measured(destination)
    if sys.platform == "darwin":  # the supported macOS data volume is APFS: the clone path is taken
        assert seen == [True]
    else:
        assert seen in ([True], [False])  # Btrfs/XFS-reflink clone, or the streamed copy elsewhere
    src, dst = os.stat(source), os.stat(destination)
    assert (src.st_dev, src.st_ino) != (dst.st_dev, dst.st_ino) and dst.st_nlink == 1  # never a hard link


def test_a_later_write_to_the_source_never_changes_the_copy(tmp_path):
    source, destination = tmp_path / "result.txt", tmp_path / "out" / "result.txt"
    source.write_bytes(b"first bytes\n" * 4096)
    measured = artifacts.copy_artifact_file(source, destination)
    with source.open("r+b") as handle:  # an in-place rewrite of the source's own blocks
        handle.seek(0)
        handle.write(b"CHANGED")
    with source.open("ab") as handle:
        handle.write(b"appended\n")
    assert _measured(destination) == measured
    assert destination.read_bytes() == b"first bytes\n" * 4096


@pytest.mark.parametrize("refusal", ["false", "oserror"])
def test_an_unavailable_clone_falls_back_to_the_streamed_copy_with_the_same_identity(tmp_path, monkeypatch, refusal):
    source, destination = tmp_path / "result.bin", tmp_path / "out" / "result.bin"
    source.write_bytes(os.urandom(300_000))
    streamed, original = [], artifacts.stream_artifact_file

    def observed(path, sink=None, **kwargs):
        streamed.append(sink is not None)
        return original(path, sink, **kwargs)

    def refused(src, dst):
        if refusal == "oserror":
            raise OSError("clone unavailable")
        return False

    monkeypatch.setattr(artifact_storage, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(artifact_storage, "_clonefile", refused)
    monkeypatch.setattr(artifacts, "stream_artifact_file", observed)
    measured = artifacts.copy_artifact_file(source, destination, expected=_measured(source))
    assert streamed == [True] and measured == _measured(destination) == _measured(source)
    assert list(destination.parent.iterdir()) == [destination]


def test_a_cloned_temporary_of_an_old_source_is_in_flight_for_the_temp_sweep(tmp_path, monkeypatch):
    """Both directions. A clone copies the source's times, so the temporary of a copy of an old result
    looked older than the sweep's age guard while its verification read was still running, and the
    maintenance child's ``temp_sweep`` reaped it from under the copy. The temporary now carries the
    clone's own time: the sweep run during that read keeps it and reaps the genuine orphan beside it,
    the copy completes, and the published file still carries the source's times."""
    from ouroboros.temp_sweep import atomic_temp_path, sweep_stale_temp_files

    root, aged = tmp_path / "data", time.time() - 7200
    source = root / "old_result.bin"
    source.parent.mkdir()
    source.write_bytes(os.urandom(200_000))
    os.utime(source, (aged, aged))
    destination = root / "task_results" / "artifacts" / "t1" / "result.bin"
    destination.parent.mkdir(parents=True)
    orphan = atomic_temp_path(destination.parent / "earlier.bin")  # a hard kill's leftover, genuinely old
    orphan.write_bytes(b"half a copy")
    os.utime(orphan, (aged, aged))
    swept, original = {}, artifacts.stream_artifact_file

    def sweep_while_the_temporary_is_in_flight(path, sink=None, **kwargs):
        temporary = pathlib.Path(sink.name if sink is not None else path)  # streamed into, or cloned then verified
        if temporary.parent == destination.parent and "temporary" not in swept:
            swept["temporary"] = temporary.name
            swept["removed"] = sweep_stale_temp_files(root, scripts=False)
            swept["kept"] = temporary.exists()
        return original(path, sink, **kwargs)

    monkeypatch.setattr(artifacts, "stream_artifact_file", sweep_while_the_temporary_is_in_flight)
    measured = artifacts.copy_artifact_file(source, destination, expected=_measured(source))
    assert swept["temporary"].startswith(".result.bin.tmp.") and (swept["removed"], swept["kept"]) == (1, True)
    assert not orphan.exists() and list(destination.parent.iterdir()) == [destination]
    assert measured == _measured(destination) == _measured(source)
    assert abs(destination.stat().st_mtime - aged) < 2  # the published copy keeps the source's times
    direct = atomic_temp_path(destination.parent / "direct.bin")
    if artifact_storage.clone_file(source, direct):  # the clone seam itself, where the platform copies the times
        assert time.time() - direct.stat().st_mtime < 600


@pytest.mark.parametrize("damaged", ["source", "clone"])
def test_clone_identity_failure_keeps_source_provenance_and_previous_destination(tmp_path, monkeypatch, damaged):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"captured bytes")
    expected = _measured(source)
    destination.write_bytes(b"previous result")
    if damaged == "source":
        source.write_bytes(b"changed bytes")

    def cloned(src, dst):
        dst.write_bytes(b"damaged clone" if damaged == "clone" else src.read_bytes())
        return True

    monkeypatch.setattr(artifacts, "clone_file", cloned)
    with pytest.raises(artifacts.ArtifactIdentityError) as failure:
        artifacts.copy_artifact_file(source, destination, expected=expected)
    if damaged == "source":
        assert failure.value.source_path == source
        stamp = source.stat()
        assert failure.value.source_stamp == (stamp.st_dev, stamp.st_ino, stamp.st_size,
                                               stamp.st_mtime_ns, stamp.st_ctime_ns)
    else:
        assert failure.value.source_path != source  # a bad clone never condemns a valid source
    assert destination.read_bytes() == b"previous result"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["destination", "source"]


def test_identity_verification_is_unchanged_on_the_clone_path(tmp_path):
    source, destination = tmp_path / "result.bin", tmp_path / "out" / "result.bin"
    source.write_bytes(b"x" * 10_000)
    with pytest.raises(artifacts.ArtifactIdentityError, match="sha256"):
        artifacts.copy_artifact_file(source, destination, expected={"size": 10_000, "sha256": "0" * 64})
    assert not destination.exists()
    assert not destination.parent.exists() or not [p for p in destination.parent.iterdir() if p.name.startswith(".")]
    measured = artifacts.copy_artifact_file(source, destination, expected=_measured(source))
    assert measured == _measured(destination)


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_a_source_that_is_not_a_regular_file_is_refused_before_any_clone(tmp_path, monkeypatch, kind):
    """A directory would be cloned whole (and its temporary could not be unlinked), a writerless FIFO's
    open would block: neither reaches the clone; the streamed copy's typed refusal answers, nothing is
    left in the destination folder, and a regular file beside them still clones."""
    import stat
    import threading

    source, destination = tmp_path / "source", tmp_path / "out" / "result"
    if kind == "directory":
        source.mkdir()
        (source / "inner.txt").write_bytes(b"inner")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFOs on this platform")
        os.mkfifo(source)
    bindings = []
    for name in ("_clonefile", "_ficlone"):
        monkeypatch.setattr(artifact_storage, name, lambda *a, _n=name: bindings.append(_n) or pytest.fail(f"{_n} ran"))
    outcome = {}

    def attempt():
        try:
            artifacts.copy_artifact_file(source, destination)
        except OSError as exc:
            outcome["error"] = str(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "the copy blocked on a source that is not a regular file"
    assert "not a regular file" in outcome["error"] and bindings == []
    assert artifact_storage.clone_file(source, tmp_path / "clone") is False and not (tmp_path / "clone").exists()
    assert not destination.exists() and list(destination.parent.iterdir()) == []
    regular = tmp_path / "regular.bin"
    regular.write_bytes(b"bytes" * 1000)
    monkeypatch.undo()
    measured = artifacts.copy_artifact_file(regular, destination, expected=_measured(regular))
    assert measured == _measured(destination) and stat.S_ISREG(destination.stat().st_mode)


def test_the_copy_temporary_carries_the_signature_the_temp_sweep_reaps(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.bin", tmp_path / "out" / "source.bin"
    source.write_bytes(b"x" * 4096)
    names = []
    monkeypatch.setattr(artifacts, "clone_file", lambda src, dst: names.append(dst.name) or False)
    artifacts.copy_artifact_file(source, destination, expected=_measured(source))
    [name] = names
    assert name.startswith(".source.bin.tmp.") and not set(name.rsplit(".tmp.", 1)[1]) - set("0123456789abcdef.")


def test_a_refused_linux_clone_leaves_no_empty_destination(tmp_path, monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("FICLONE is a Linux ioctl")
    import fcntl

    source, destination = tmp_path / "a", tmp_path / "b"
    source.write_bytes(b"bytes")
    monkeypatch.setattr(fcntl, "ioctl", lambda *a: (_ for _ in ()).throw(OSError(95, "not supported")))
    assert artifact_storage.clone_file(source, destination) is False and not destination.exists()
