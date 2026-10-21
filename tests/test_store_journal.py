"""WAL needs a fixed SQLite, enforced locks AND a positively identified local filesystem."""
import contextlib
import ctypes
import os
import pathlib
import shutil
import sqlite3
import sys
import threading

import pytest

from ouroboros import store, store_import, store_switch, usage_store
from tests.test_store import publish


@pytest.fixture(autouse=True)
def release_connections():
    store.close_connections()
    yield
    store.close_connections()


@pytest.mark.parametrize("version,fixed", [
    ((3, 37, 2), False), ((3, 44, 5), False), ((3, 44, 6), True), ((3, 44, 9), True),
    ((3, 45, 9), False), ((3, 50, 4), False), ((3, 50, 6), False), ((3, 50, 7), True),
    ((3, 50, 8), True), ((3, 51, 2), False), ((3, 51, 3), True), ((3, 53, 1), True)])
@pytest.mark.parametrize("tier", [store.TIER_ENFORCED, store.TIER_NAME])
@pytest.mark.parametrize("filesystem,local", [
    ("apfs", True), ("ext4", True), ("fixed", True), ("nfs", False), ("cifs", False),
    ("fuse.drive", False), ("9p", False), ("unknown", False), (None, False)])
def test_wal_requires_all_three_facts(monkeypatch, version, fixed, tier, filesystem, local):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    monkeypatch.setattr(store, "_filesystem_type", lambda _path: filesystem)
    assert store._journal_mode(pathlib.Path("store.sqlite"), tier) == (
        "WAL" if fixed and tier == store.TIER_ENFORCED and local else "PERSIST")


@pytest.mark.parametrize("tier,version", [(store.TIER_NAME, (3, 53, 1)), (store.TIER_ENFORCED, (3, 50, 4))])
def test_ineligible_engine_or_tier_never_probes_filesystem(monkeypatch, tier, version):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    monkeypatch.setattr(store, "_filesystem_type", lambda _: pytest.fail("unnecessary filesystem probe"))
    assert store._journal_mode(pathlib.Path("store.sqlite"), tier) == "PERSIST"


def _mountinfo_path(path: pathlib.Path) -> str:
    """A mount point as the kernel writes it in mountinfo: space, tab, newline and backslash as octal
    escapes. The parser only runs on Linux, but this test runs everywhere: a Windows tmp_path carries
    backslashes and a drive anchor, so its root mount is that anchor rather than ``/``."""
    return str(path).translate({ord(" "): r"\040", ord("\t"): r"\011", ord("\n"): r"\012", ord("\\"): r"\134"})


@pytest.mark.parametrize("kind,expected", [
    ("ext4", "ext4"), ("xfs", "xfs"), ("nfs", "nfs"), ("nfs4", "nfs4"), ("cifs", "cifs"),
    ("smb3", "smb3"), ("fuse", "fuse"), ("fuseblk", "fuseblk"), ("fuse.drive", "fuse.drive"), ("9p", "9p")])
def test_linux_uses_longest_mount_including_bind_and_escaped_names(tmp_path, kind, expected):
    mount = tmp_path.resolve() / "nested mount"
    mount.mkdir()
    root, escaped = _mountinfo_path(pathlib.Path(mount.anchor)), _mountinfo_path(mount)
    table = f"1 0 8:1 / {root} rw - ext4 /dev/root rw\n2 1 0:2 /bind {escaped} rw - {kind} source rw\n"
    assert store._linux_filesystem(mount / "store.sqlite", table) == expected
    assert store._linux_filesystem(tmp_path / "elsewhere.sqlite", table) == "ext4"
    assert store._linux_filesystem(mount, "bad mountinfo") is None
    assert store._linux_filesystem(mount, "") is None


@pytest.mark.skipif(os.name == "nt", reason="overlay options separate directories with ':', which a Windows "
                    "drive path carries; this parser runs only on Linux")
@pytest.mark.parametrize("backing", ["ext4", "nfs", "fuse.drive", "missing"])
def test_overlay_requires_every_backing_directory_to_be_known_local(tmp_path, backing):
    overlay, lower, upper = [tmp_path / part for part in ("overlay", "lower", "upper")]
    for directory in (overlay, lower, upper):
        directory.mkdir()
    table = (f"1 0 8:1 / / rw - ext4 /dev/root rw\n"
             f"2 1 0:2 / {lower} rw - {backing} source rw\n"
             f"3 1 0:3 / {overlay} rw - overlay overlay rw,lowerdir={lower},upperdir={upper}\n")
    assert store._linux_filesystem(overlay / "store.sqlite", table) == ("overlay-local" if backing == "ext4" else None)
    hidden = "1 0 0:1 / / rw - overlay overlay rw,lowerdir=/host/invisible\n"
    assert store._linux_filesystem(tmp_path, hidden) is None


@pytest.mark.parametrize("platform,kind,expected", [
    ("darwin", "apfs", "apfs"), ("darwin", "hfs", "hfs"), ("darwin", "smbfs", None),
    ("freebsd14", "ufs", "ufs"), ("freebsd14", "zfs", None), ("freebsd14", "nfs", None)])
def test_bsd_statfs_reads_the_native_filesystem_name(monkeypatch, tmp_path, platform, kind, expected):
    class StatFS:
        def __call__(self, path, pointer):
            record = pointer._obj
            assert type(record).fstypename.offset == (72 if platform == "darwin" else 280)
            record.fstypename = kind.encode("ascii")
            return 0
    class LibC:
        statfs64 = StatFS()
        statfs = StatFS()
    monkeypatch.setattr(store.sys, "platform", platform)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_kw: LibC())
    assert store._filesystem_type(tmp_path) == expected


@pytest.mark.parametrize("kind", [0, 1, 2, 3, 4, 5, 6])
def test_windows_only_accepts_fixed_volume_including_volume_mounts(monkeypatch, tmp_path, kind):
    class Volume:
        def __call__(self, path, root, size):
            root.value = "C:\\mounted-volume\\"
            return 1
    class Drive:
        def __call__(self, root):
            assert root == "C:\\mounted-volume\\"
            return kind
    class Kernel:
        GetVolumePathNameW = Volume()
        GetDriveTypeW = Drive()
    monkeypatch.setattr(store.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_kw: Kernel(), raising=False)
    assert store._filesystem_type(tmp_path) == ("fixed" if kind == 3 else None)


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32", "unknown"])
def test_unreadable_filesystem_is_unknown(monkeypatch, tmp_path, platform):
    def unavailable(*_args, **_kwargs):
        raise OSError("filesystem unavailable")
    monkeypatch.setattr(store.sys, "platform", platform)
    monkeypatch.setattr(pathlib.Path, "read_text", unavailable)
    monkeypatch.setattr(store, "_bsd_filesystem", unavailable)
    monkeypatch.setattr(store, "_windows_filesystem", unavailable)
    assert store._filesystem_type(tmp_path) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS statfs integration")
def test_native_statfs_identifies_the_test_volume(tmp_path):
    # Synthetic data, but the real ABI: an offset or symbol error must not silently
    # disable WAL on the APFS/HFS test host.
    assert store._bsd_filesystem(tmp_path) in {"apfs", "hfs"}


@pytest.mark.parametrize("money", [False, True])
def test_wal_is_full_and_export_checkpoints_to_standalone_delete(tmp_path, monkeypatch, money):
    # The shipped interpreter may predate the fix. Override eligibility only for
    # this serial synthetic lifecycle; the production version gate above stays strict.
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 53, 1))
    monkeypatch.setattr(store, "_filesystem_type", lambda _path: "apfs")
    if money:
        usage_store.migrate_from_journal(tmp_path)
    else:
        publish(tmp_path)
    with usage_store.hold(tmp_path) if money else store.open_store(tmp_path, write=True) as txn:
        txn.set_meta("retained", 42)
        assert txn.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert txn.conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        held = txn.conn
    path = tmp_path / (usage_store.STORE_REL if money else store.STORE_REL)
    assert path.with_name(path.name + "-wal").exists()
    if money:
        result = usage_store.export_journal(tmp_path)
        retired = path.with_name(result["store_retired_as"])
    else:
        with store.open_store(tmp_path, write=True) as txn:
            txn.set_meta("phase", store.EXPORTING)
        store_import.export_facts(tmp_path)
        retired = path.with_name(store_switch._retire(tmp_path))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        held.execute("SELECT 1")
    assert all(not path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    with contextlib.closing(sqlite3.connect(retired)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT value FROM meta WHERE key='retained'").fetchone()[0] == "42"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("money", [False, True])
@pytest.mark.parametrize("mode", ["PERSIST", "WAL"])
def test_sibling_build_is_standalone_before_publication(tmp_path, monkeypatch, money, mode):
    monkeypatch.setattr(store, "_journal_mode", lambda *_: mode)
    if money:
        usage_store.migrate_from_journal(tmp_path)
    else:
        store_import.ensure_current_store(tmp_path, with_children=False)
    assert list((tmp_path / "state").glob("*.import-*")) == []


@pytest.mark.parametrize("mode", ["PERSIST", "WAL"])
def test_failed_build_rolls_back_and_releases_its_temporary_journal(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(store, "_journal_mode", lambda *_: mode)
    path = tmp_path / "unpublished.sqlite"
    with pytest.raises(ValueError):
        with contextlib.closing(store.connect(path, store.TIER_ENFORCED, create=True)) as conn:
            conn.execute("CREATE TABLE pending(value)")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO pending VALUES (1)")
            raise ValueError("interrupted build")
    assert all(not path.with_name(path.name + suffix).exists() for suffix in ("-journal", "-wal", "-shm"))
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT * FROM pending").fetchall() == []
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


@pytest.mark.parametrize("dialogue", [False, True])
@pytest.mark.parametrize("wal", [True, False])
def test_a_door_reset_hands_every_store_off_out_of_wal_first(tmp_path, monkeypatch, wal, dialogue):
    # A store-aware older body never sets the journal mode: it must receive DELETE files, from the
    # door's own reset and from the seam alike (every release before the dialogue store needs the
    # seam), while a rollback-journal store needs no connection at all.
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 53, 1))
    monkeypatch.setattr(store, "_filesystem_type", lambda _path: "apfs" if wal else None)
    publish(tmp_path)
    usage_store.migrate_from_journal(tmp_path)
    with store.open_store(tmp_path, write=True) as txn:
        txn.set_meta("retained", 1)
    with usage_store.hold(tmp_path) as txn:
        txn.set_meta("retained", 1)
    paths = [tmp_path / store.STORE_REL, tmp_path / usage_store.STORE_REL]
    assert [store.wal_file(path) for path in paths] == [wal, wal]
    if not wal:
        monkeypatch.setattr(store, "prepare_handoff", lambda *a, **k: pytest.fail("rollback journals need no handoff"))
    monkeypatch.setattr(store_switch, "switch_needs",
                        lambda *_: {"facts": False, "money": False, "handles": False, "dialogue": dialogue})
    monkeypatch.setattr(store_switch, "rev_parse", lambda _repo, _ref: "0" * 40)
    seen = []

    def reset(*_intent):
        seen.append([store.wal_file(path) for path in paths])
        return True, "reset done"

    ok, _message, deferred = store_switch.switch_or_reset(tmp_path, tmp_path, target_sha="0" * 40, target_ref="v-old",
                                                          reason="test", door="ui_rollback", reset=reset)
    assert (ok, deferred) == (True, dialogue)
    if deferred:  # the seam exports and moves the checkout at the restart exit, the door as its owner
        assert [store.wal_file(path) for path in paths] == [wal, wal]  # nothing moved yet
        store.close_connections()
        assert store_switch.run_switch_seam(tmp_path, repo_dir=tmp_path, owner=reset)["status"] == "switched"
    assert seen == [[False, False]]
    assert all(not path.with_name(path.name + "-wal").exists() for path in paths)
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.meta("retained") == 1


@pytest.mark.parametrize("outcome", ["refused_before_tree_effects", "failed", "raised"])
def test_a_reset_that_changed_nothing_hands_the_stores_back(tmp_path, monkeypatch, outcome):
    """After a successful hand-off: a reset refused before any tree effect leaves this process running
    on its own stores, which choose their journal again; any other failure may already have moved
    the checkout, so the stores stay handed off."""
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 53, 1))
    monkeypatch.setattr(store, "_filesystem_type", lambda _path: "apfs")
    publish(tmp_path)
    with store.open_store(tmp_path, write=True) as txn:
        txn.set_meta("retained", 1)
    monkeypatch.setattr(store_switch, "switch_needs", lambda *_: {"facts": False, "money": False})
    handed = os.path.abspath(store.store_path(tmp_path))

    class Refused(Exception):
        before_tree_effects = outcome == "refused_before_tree_effects"

    def reset():
        assert handed in store._HANDED_OFF
        if outcome == "failed":
            return False, "the reset failed"
        raise Refused("git refused the reset")

    if outcome == "failed":
        assert store_switch.switch_or_reset(tmp_path, tmp_path, target_sha="0" * 40, target_ref="v-old", reason="t",
                                            door="ui_rollback", reset=reset) == (False, "the reset failed", False)
    else:
        with pytest.raises(Refused):
            store_switch.switch_or_reset(tmp_path, tmp_path, target_sha="0" * 40, target_ref="v-old", reason="t",
                                         door="ui_rollback", reset=reset)
    assert (handed in store._HANDED_OFF) is (outcome != "refused_before_tree_effects")
    store.forget_handoffs()


def test_a_store_that_cannot_hand_off_refuses_the_door_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 53, 1))
    monkeypatch.setattr(store, "_filesystem_type", lambda _path: "apfs")
    publish(tmp_path)
    with store.open_store(tmp_path, write=True) as txn:
        txn.set_meta("retained", 1)
    store.close_connections()

    def busy(*_args, **_kwargs):
        raise store.StoreBusy("store checkpoint busy at handoff", reason="contention")

    monkeypatch.setattr(store, "prepare_handoff", busy)
    monkeypatch.setattr(store_switch, "switch_needs", lambda *_: {"facts": False, "money": False})
    handed = os.path.abspath(store.store_path(tmp_path))
    store._HANDED_OFF.add(handed)  # a store the refused door had already handed off
    ok, message, deferred = store_switch.switch_or_reset(
        tmp_path, tmp_path, target_sha="0" * 40, target_ref="v-old", reason="test", door="ui_rollback",
        reset=lambda: pytest.fail("the checkout moved past a store still in WAL"))
    assert (ok, deferred) == (False, False) and "checkpoint busy" in message
    assert handed not in store._HANDED_OFF, "a refused door hands every store back to this process"


def test_the_log_identity_moves_with_every_commit(tmp_path):
    """Read from the log's header and frame headers beside its size and time: a commit adds a commit
    frame, a rolled-back transaction's spilled frames add none (only the log's size and time move),
    and a log reused in place at the same size after a checkpoint carries a new generation's salts."""
    path = tmp_path / "log.sqlite"
    writer = sqlite3.connect(path, isolation_level=None)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t(v)")
    held = sqlite3.connect(path, isolation_level=None)  # stays open: no last close removes the log
    log = path.with_name(path.name + "-wal")

    def commit(rows, connection=writer):
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany("INSERT INTO t VALUES (?)", [("x" * 900,)] * rows)
        connection.execute("COMMIT")
        return store.log_identity(path)

    first = commit(20)
    assert first[1] > 1 and commit(1)[1] > first[1]  # every commit adds its frames
    committed = store.log_identity(path)
    spiller = sqlite3.connect(path, isolation_level=None)
    spiller.execute("PRAGMA cache_size = 5")  # the transaction spills frames into the log before it ends
    spiller.execute("BEGIN IMMEDIATE")
    spiller.executemany("INSERT INTO t VALUES (?)", [("y" * 3000,)] * 300)
    spiller.execute("ROLLBACK")
    spilled = store.log_identity(path)
    assert log.stat().st_size > 32 + 4096 * committed[1] and spilled[:3] == committed[:3] and spilled[3] > committed[3]
    small = commit(1)
    assert small[0] == committed[0] and small[1] > committed[1]  # over the spilled frames, one commit further
    assert writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0] == 0
    size = log.stat().st_size
    reused = commit(1)  # every frame is in the database: the log restarts in place
    assert log.stat().st_size == size and reused[0] != small[0] and reused[1] < small[1]
    for connection in (spiller, held, writer):
        connection.close()


def test_a_killed_writer_s_unseen_commit_frame_does_not_hide_the_next_commit(tmp_path):
    """A writer killed between writing its commit frame and publishing it in the index leaves a frame
    that looks committed, which no reader sees while another connection keeps the index alive; the
    next commit of the same length overwrites it at the same frame number (Opus reproduced it with
    SIGKILL). Here the orphan is a real commit frame of the same update, taken from a copy of the
    store, so the frame and the salts stay the same: the frame's checksum moves the identity even
    while the log's size and time stay, as a coarse or a held file's time may."""
    path = tmp_path / "log.sqlite"
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as setup:
        setup.execute("CREATE TABLE t(v)")
        setup.execute("INSERT INTO t VALUES ('start')")
    writer = store.connect(path, store.TIER_ENFORCED, journal_mode="WAL")  # counted: its file reads borrow
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("UPDATE t SET v = 'committed'")
    held = sqlite3.connect(path, isolation_level=None)  # keeps the index: no recovery adopts the orphan
    assert held.execute("SELECT v FROM t").fetchone() == ("committed",)
    log = path.with_name(path.name + "-wal")
    published = store.log_identity(path)
    copy = tmp_path / "copy.sqlite"
    copy.write_bytes(store.read_file_bytes(path))
    shutil.copyfile(log, copy.with_name(copy.name + "-wal"))
    with contextlib.closing(sqlite3.connect(copy, isolation_level=None)) as other:  # recovers the same log
        other.execute("PRAGMA wal_autocheckpoint = 0")
        other.execute("UPDATE t SET v = 'killed'")  # the killed writer's commit, one frame further
        orphan = copy.with_name(copy.name + "-wal").read_bytes()[log.stat().st_size:]
    with open(log, "r+b") as raw:  # it reached the log, never the index
        raw.seek(log.stat().st_size)
        raw.write(orphan)
    seen, size, written = store.log_identity(path), log.stat().st_size, log.stat().st_mtime_ns
    assert seen[1] == published[1] + 1 and held.execute("SELECT v FROM t").fetchone() == ("committed",)
    writer.execute("UPDATE t SET v = 'next'")  # the next commit of the same length overwrites the orphan
    os.utime(log, ns=(written, written))  # the log's time as a coarse or held file time keeps it
    after = store.log_identity(path)
    assert held.execute("SELECT v FROM t").fetchone() == ("next",) and log.stat().st_size == size
    assert after[:2] == seen[:2], "the same frame of the same generation"
    assert after != seen, "the commit frame's checksum must move the identity"
    for connection in (held, writer):
        connection.close()


def test_a_shorter_commit_over_a_killed_writer_s_longer_one_moves_only_the_log_s_time(tmp_path):
    """A killed writer's unseen commit spans four frames, and the next commit writes one: the
    unseen commit frame stays the log's last, so the frame, its checksum and the log's size stay the
    same, and only the log's time moves the identity (pinned: with the time constant, it would not
    move at all; on a volume that keeps a held file's time it stays until the next commit)."""
    path = tmp_path / "log.sqlite"
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as setup:
        setup.execute("CREATE TABLE t(k INTEGER PRIMARY KEY, v)")
        setup.executemany("INSERT INTO t VALUES (?, ?)", [(k, "a" * 3000) for k in range(4)])  # a leaf page each
    writer = store.connect(path, store.TIER_ENFORCED, journal_mode="WAL")  # counted: its file reads borrow
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("UPDATE t SET v = 'b' || substr(v, 2) WHERE k = 0")
    held = sqlite3.connect(path, isolation_level=None)  # keeps the index: no recovery adopts the orphan
    log = path.with_name(path.name + "-wal")
    published = store.log_identity(path)
    copy = tmp_path / "copy.sqlite"
    copy.write_bytes(store.read_file_bytes(path))
    shutil.copyfile(log, copy.with_name(copy.name + "-wal"))
    with contextlib.closing(sqlite3.connect(copy, isolation_level=None)) as other:  # recovers the same log
        other.execute("PRAGMA wal_autocheckpoint = 0")
        other.execute("UPDATE t SET v = 'k' || substr(v, 2)")  # the killed writer's commit: four leaf pages
        orphan = copy.with_name(copy.name + "-wal").read_bytes()[log.stat().st_size:]
    with open(log, "r+b") as raw:  # it reached the log, never the index
        raw.seek(log.stat().st_size)
        raw.write(orphan)
    then = log.stat().st_mtime_ns - 10 ** 10
    os.utime(log, ns=(then, then))  # written ten seconds ago, past any file time's grain
    seen = store.log_identity(path)
    rows = "SELECT group_concat(substr(v, 1, 1), '') FROM t"
    assert seen[1] - published[1] == 4 and held.execute(rows).fetchone() == ("baaa",)  # the orphan unseen
    writer.execute("UPDATE t SET v = 'n' || substr(v, 2) WHERE k = 3")  # one frame: shorter than the orphan
    moved = store.log_identity(path)
    assert held.execute(rows).fetchone() == ("baan",)
    assert [part for part, (now, before) in enumerate(zip(moved, seen)) if now != before] == [4], (seen, moved)
    for connection in (held, writer):
        connection.close()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_in_the_log_s_place_is_never_opened(tmp_path):
    """Opening a FIFO for reading waits for a writer: the log's identity looks at a regular file only,
    as ``is_sqlite_file`` does, on the history page's request thread and in recovery alike."""
    path = tmp_path / "log.sqlite"
    fifo = path.with_name(path.name + "-wal")
    os.mkfifo(fifo)
    result = []
    reader = threading.Thread(target=lambda: result.append(store.log_identity(path)), daemon=True)
    reader.start()
    reader.join(timeout=10)
    blocked = reader.is_alive()
    if blocked:  # a writer's open ends the reader's wait
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        reader.join(timeout=10)
    assert not blocked and result == [None]


@pytest.mark.parametrize("mode", ["PERSIST", "WAL"])
def test_the_store_state_sees_a_vacuum_that_keeps_every_row(tmp_path, monkeypatch, mode):
    """``committed_content`` cannot show that nothing wrote: a VACUUM keeps every row. The store
    state also holds the write's trace, which a first WAL open's header fields do not count as."""
    from tests._store_journal_shared import committed_content, store_state, use_journal
    from tests.test_store import document

    use_journal(monkeypatch, mode)
    path = publish(tmp_path, document("t1"), document("t2"))  # a rollback-journal build, as the importer leaves it
    before = store_state(path)
    with store.open_store(tmp_path, write=False) as txn:  # the first open sets the journal up: WAL rewrites the header
        assert txn.load("t1")["status"] == "completed"
    assert store_state(path) == before
    with contextlib.closing(store.connect(path, store.TIER_ENFORCED)) as conn:
        conn.execute("VACUUM")
    assert committed_content(path) == before.content and store_state(path) != before
