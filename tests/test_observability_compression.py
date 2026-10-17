"""Hot-path compression changes storage cost, never raw CAS identity."""

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from ouroboros import observability


@pytest.mark.parametrize("kind", ["json", "txt"])
def test_level_one_blob_keeps_raw_digest_and_plain_gzip_reader(tmp_path, monkeypatch, kind):
    payload = {"content": "Repeated Ω history\n" * 1000} if kind == "json" else "Repeated Ω text\n" * 1000
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
           if kind == "json" else payload).encode("utf-8")
    levels, gzip_open = [], gzip.open

    def observed_open(path, mode, **kwargs):
        if mode == "wb":
            levels.append(kwargs.get("compresslevel"))
        return gzip_open(path, mode, **kwargs)

    monkeypatch.setattr(observability.gzip, "open", observed_open)
    ref = observability.write_blob(tmp_path, payload, kind=kind)
    assert levels == [1]
    assert ref["sha256"] == hashlib.sha256(raw).hexdigest()
    assert ref["size"] == len(raw)
    with gzip_open(ref["path"], "rb") as stream:
        assert stream.read() == raw
    assert observability.read_blob_ref(tmp_path, ref, expected_kind=kind) == payload
    path = Path(ref["path"])
    compressed, inode = path.read_bytes(), path.stat().st_ino
    assert observability.write_blob(tmp_path, payload, kind=kind) == ref
    assert levels == [1] and path.read_bytes() == compressed and path.stat().st_ino == inode


def test_existing_level_nine_blob_remains_unchanged(tmp_path):
    payload = {"content": "Existing history"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "observability" / "blobs" / f"{digest}.json.gz"
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wb", compresslevel=9) as stream:
        stream.write(raw)
    compressed, inode = path.read_bytes(), path.stat().st_ino
    ref = observability.write_blob(tmp_path, payload)
    assert ref["sha256"] == digest
    assert path.read_bytes() == compressed and path.stat().st_ino == inode
    assert observability.read_blob_ref(tmp_path, ref) == payload
