"""Bounded, passive storage-dashboard backend contracts (#1418)."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import pytest

from ouroboros import storage
from ouroboros.gateway import contracts
from ouroboros.gateway.storage import storage_routes
from ouroboros.storage import StorageObserver, measure_data_folder

try:
    import resource
except ImportError:  # pragma: no cover - Windows has no descriptor rlimit.
    resource = None


def _measure(root: Path, **kwargs):
    kwargs.setdefault("external_roots", {})
    return measure_data_folder(root, seconds=30, **kwargs)


def _row(result, category):
    return next(row for row in result["categories"] if row["id"] == category)


def _join_walks():
    """Wait for the observer's walk thread itself, not a sleep loop."""
    for thread in threading.enumerate():
        if thread.name == "storage-measure":
            thread.join(timeout=30)
            assert not thread.is_alive()


@pytest.fixture(params=["openat", "by-path"])
def walk(request, monkeypatch):
    """Both traversals: directory-relative no-follow opens, and the by-path fallback
    a platform without them (Windows) takes. Neither may follow a link."""
    if request.param == "openat" and not getattr(storage, "_RELATIVE", True):
        pytest.skip("this platform has no directory-relative no-follow open")
    monkeypatch.setattr(storage, "_RELATIVE", request.param == "openat", raising=False)
    return request.param


# --------------------------------------------------------------- what is counted


def test_categories_are_disjoint_and_hardlinks_count_once(tmp_path, walk):
    data = tmp_path / "data"
    (data / "observability").mkdir(parents=True)
    (data / "task_results").mkdir()
    first = data / "observability" / "call.json"
    first.write_bytes(b"123456789012")
    os.link(first, data / "task_results" / "same-bytes.json")
    os.link(first, data / "task_results" / "same-again.json")
    (data / "task_results" / "own.json").write_bytes(b"own")

    measured = _measure(data)

    assert (_row(measured, "observability")["bytes"], _row(measured, "observability")["files"]) == (12, 1)
    results = _row(measured, "results")
    assert (results["bytes"], results["files"], results["coverage"]) == (3, 1, "complete")
    assert measured["measured_bytes"] == 15


def test_links_inside_a_category_are_never_counted_or_walked(tmp_path, walk):
    data, outside = tmp_path / "data", tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 1000)
    root = data / "task_results"
    root.mkdir(parents=True)
    (root / "own.bin").write_bytes(b"own")
    (root / "file-link").symlink_to(outside / "big.bin")
    (root / "dir-link").symlink_to(outside, target_is_directory=True)

    row = _row(_measure(data), "results")

    assert (row["bytes"], row["files"], row["coverage"]) == (3, 1, "complete")


def test_symlink_category_root_is_unknown_and_target_is_not_walked(tmp_path, walk):
    data, outside = tmp_path / "data", tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"must not count")
    data.mkdir()
    (data / "observability").symlink_to(outside, target_is_directory=True)

    measured = _measure(data)

    row = _row(measured, "observability")
    assert row["bytes"] is None
    assert row["coverage"] == "unreadable"
    assert measured["measured_bytes"] == 0


def test_category_root_behind_a_linked_ancestor_is_unknown_and_never_walked(tmp_path, walk):
    """``state/headless_tasks`` and ``state/skills`` under a linked ``state``: a no-follow
    open of the last component alone would still pass through the linked ancestor."""
    data, outside = tmp_path / "data", tmp_path / "outside"
    for name in ("headless_tasks", "skills"):
        (outside / name).mkdir(parents=True)
        (outside / name / "secret.bin").write_bytes(b"x" * 100)
    data.mkdir()
    (data / "state").symlink_to(outside, target_is_directory=True)

    measured = _measure(data)

    for category in ("task_drives", "skills"):
        row = _row(measured, category)
        assert (row["bytes"], row["coverage"]) == (None, "unreadable"), row
    assert measured["measured_bytes"] == 0


# ----------------------------------------------------- configured exclusions


def _excluded_layout(data: Path) -> None:
    for rel, size in (("state/headless_tasks/h.bin", 11), ("state/skills/s.bin", 13),
                      ("task_drives/one/owned.bin", 5), ("task_drives/one/proj/outside.bin", 7)):
        path = data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


@pytest.mark.parametrize(("case", "task_drives", "skills"), [
    ("none", 23, 13),
    ("exact_category_root", 11, 13),
    ("ancestor_of_category_roots", 12, 0),
    ("descendant", 16, 13),
    ("outside_link_alias", 16, 13),
    ("case_alias", 16, 13),
])
def test_configured_roots_are_excluded_wherever_they_sit(tmp_path, walk, case, task_drives, skills):
    data = tmp_path / "data"
    _excluded_layout(data)
    configured = {
        "exact_category_root": data / "task_drives",
        "ancestor_of_category_roots": data / "state",
        "descendant": data / "task_drives" / "one" / "proj",
        "outside_link_alias": tmp_path / "alias",
        "case_alias": data / "TASK_DRIVES" / "one" / "PROJ",
    }.get(case)
    if case == "outside_link_alias":
        configured.symlink_to(data / "task_drives" / "one" / "proj", target_is_directory=True)
    if case == "case_alias" and not configured.is_dir():
        pytest.skip("case-sensitive filesystem: the alias names nothing")

    measured = measure_data_folder(
        data, external_roots={"project_folders": str(configured) if configured else ""}, seconds=30,
    )

    assert _row(measured, "task_drives")["bytes"] == task_drives
    assert _row(measured, "skills")["bytes"] == skills
    assert _row(measured, "other")["bytes"] == 0
    # Exclusion is disclosed as "not measured", not as a partial count.
    assert all(row["coverage"] == "complete" for row in measured["categories"])
    assert "project_folders" in measured["not_measured"]


# ------------------------------------------------------------------ bounds


def test_entry_limit_is_visible_as_partial_lower_bound(tmp_path):
    data = tmp_path / "data"
    root = data / "observability"
    root.mkdir(parents=True)
    (root / "one").write_bytes(b"one")
    (root / "two").write_bytes(b"two")

    measured = _measure(data, entries=1)

    row = _row(measured, "observability")
    assert (row["stopped"], row["coverage"], row["bytes"]) == ("entry_limit", "partial", 3)
    assert measured["complete"] is False


def test_time_bound_is_checked_before_each_listing(tmp_path, monkeypatch):
    """Empty folders have no entries to check the clock on: a slow listing of each must
    still stop the walk once the category's time is spent."""
    data = tmp_path / "data"
    for name in ("a", "b", "c"):
        (data / "observability" / name).mkdir(parents=True)
    now = [0.0]
    real = os.scandir

    def slow_listing(path):
        now[0] += 10.0
        return real(path)

    monkeypatch.setattr(os, "scandir", slow_listing)
    measured = measure_data_folder(data, external_roots={}, seconds=15, clock=lambda: now[0])

    row = _row(measured, "observability")
    assert (row["stopped"], row["coverage"]) == ("time_limit", "partial"), row


def test_entry_bound_is_checked_before_each_listing(tmp_path):
    data = tmp_path / "data"
    for name in ("a", "b", "c"):
        (data / "observability" / name).mkdir(parents=True)

    row = _row(_measure(data, entries=3), "observability")

    # Three entries were the whole budget: the third folder is not listed on top of it.
    assert (row["stopped"], row["coverage"]) == ("entry_limit", "partial"), row


@pytest.mark.skipif(resource is None or not os.path.isdir("/dev/fd"), reason="needs a descriptor rlimit")
def test_wide_folder_is_measured_under_a_low_descriptor_limit(tmp_path):
    """The walk keeps only its current path open: 300 sibling folders need no more
    descriptors than one, so a low limit still yields a complete count."""
    data = tmp_path / "data"
    for index in range(300):
        folder = data / "task_results" / f"t{index}"
        folder.mkdir(parents=True)
        (folder / "r.bin").write_bytes(b"x")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    in_use = max(int(name) for name in os.listdir("/dev/fd"))
    limit = in_use + 32
    if hard != resource.RLIM_INFINITY and limit > hard:
        pytest.skip("hard descriptor limit too low to leave headroom")
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
    try:
        measured = _measure(data)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    row = _row(measured, "results")
    assert (row["bytes"], row["files"], row["errors"], row["coverage"]) == (300, 300, 0, "complete"), row


def test_depth_bound_skips_deeper_folders_and_says_so(tmp_path, walk):
    data = tmp_path / "data"
    deep = data / "memory" / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (data / "memory" / "a" / "top.bin").write_bytes(b"12")
    (deep / "deep.bin").write_bytes(b"123456")

    measured = _measure(data, depth=2)

    row = _row(measured, "memory")
    assert (row["bytes"], row["skipped"], row["coverage"]) == (2, 1, "partial"), row
    assert measured["complete"] is False


# --------------------------------------------------- platform guarantees


def test_a_folder_swapped_for_a_link_mid_walk_is_never_listed_through_it(tmp_path, monkeypatch, walk):
    """Between reading an entry and listing it, the folder becomes a link to elsewhere."""
    data, outside = tmp_path / "data", tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"x" * 100)
    victim = data / "task_results" / "victim"
    victim.mkdir(parents=True)
    (data / "task_results" / "own.bin").write_bytes(b"own")
    real = storage._listing_stat

    def swap_after_stat(entry, directory_fd):
        info = real(entry, directory_fd)
        if entry.name == "victim" and not victim.is_symlink():
            victim.rmdir()
            victim.symlink_to(outside, target_is_directory=True)
        return info

    monkeypatch.setattr(storage, "_listing_stat", swap_after_stat)
    row = _row(_measure(data), "results")

    assert (row["bytes"], row["errors"], row["coverage"]) == (3, 1, "partial"), row


def _identityless(info, *, nlink=0):
    """A Windows-shaped stat: ``DirEntry.stat()`` there carries no inode, device or link count."""
    # Preserve Windows' reparse metadata; the ten-field constructor leaves it None.
    extra = {name: getattr(info, name) for name in ("st_file_attributes", "st_reparse_tag")
             if hasattr(info, name)}
    return os.stat_result((info.st_mode, 0, 0, nlink, info.st_uid, info.st_gid, info.st_size,
                           int(info.st_atime), int(info.st_mtime), int(info.st_ctime)), extra)


def test_link_identity_missing_from_the_listing_is_read_from_the_file_or_left_out(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "task_results").mkdir(parents=True)
    first = data / "task_results" / "a.bin"
    first.write_bytes(b"abcd")
    os.link(first, data / "task_results" / "b.bin")
    (data / "task_results" / "single.bin").write_bytes(b"one")
    listing_stat = storage._listing_stat
    monkeypatch.setattr(storage, "_listing_stat", lambda entry, fd: _identityless(listing_stat(entry, fd)))

    row = _row(_measure(data), "results")
    assert (row["bytes"], row["files"], row["skipped"], row["coverage"]) == (7, 2, 0, "complete"), row

    # A filesystem that reports link counts but no file identity at all: a multiply linked
    # file cannot be told from its twin, so both stay out and the row says it is partial.
    def no_identity(path):
        info = os.lstat(path)
        return _identityless(info, nlink=info.st_nlink)

    monkeypatch.setattr(storage, "_path_stat", no_identity)
    row = _row(_measure(data), "results")
    assert (row["bytes"], row["files"], row["skipped"], row["coverage"]) == (3, 1, 2, "partial"), row


# ---------------------------------------------------- observer and routes


def _fake_measurement(measured_bytes):
    return {"finished_at": "b", "duration_seconds": 0, "measured_bytes": measured_bytes, "complete": True,
            "free_bytes": None, "categories": [], "not_measured": []}


def test_get_projection_is_passive_and_refresh_keeps_last_result(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    calls = []

    def measure(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return _fake_measurement(4)
        raise OSError("refresh failed")

    observer = StorageObserver(data, measure=measure, external_roots=dict)
    assert observer.projection() == {"measuring": False, "measurement": None, "error": None}
    assert calls == []
    assert observer.start() is True
    _join_walks()
    assert observer.projection()["measurement"]["measured_bytes"] == 4
    assert observer.start() is True
    _join_walks()
    projection = observer.projection()
    assert projection["measurement"]["measured_bytes"] == 4
    assert projection["error"]["message"] == "The measurement failed unexpectedly."
    assert len(calls) == 2


def test_concurrent_measure_requests_share_one_walk(tmp_path):
    release, calls = threading.Event(), []

    def measure(*args, **kwargs):
        calls.append(args)
        assert release.wait(30)
        return _fake_measurement(9)

    observer = StorageObserver(tmp_path, measure=measure, external_roots=dict)
    gate, answers = threading.Barrier(16), []

    def request():
        gate.wait(30)
        answers.append(observer.start())

    requests = [threading.Thread(target=request) for _ in range(16)]
    for thread in requests:
        thread.start()
    for thread in requests:
        thread.join(30)

    assert sorted(answers) == [False] * 15 + [True]
    assert observer.projection()["measuring"] is True
    release.set()
    _join_walks()
    assert len(calls) == 1
    projection = observer.projection()
    assert projection["measuring"] is False and projection["measurement"]["measured_bytes"] == 9


def test_storage_route_surface_has_passive_get_and_explicit_post(tmp_path):
    routes = storage_routes(tmp_path)
    assert {(route.path, frozenset(route.methods or ())) for route in routes} == {
        ("/api/storage", frozenset({"GET", "HEAD"})),
        ("/api/storage/measure", frozenset({"POST"})),
    }


def test_mounted_storage_routes_read_passively_until_the_owner_measures(tmp_path, monkeypatch):
    """The routes as gateway.router mounts them: GET never lists a folder or writes one;
    only the POST starts the walk, and its result is then read back passively."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from ouroboros.gateway.router import collect_routes

    data = tmp_path / "data"
    (data / "task_results").mkdir(parents=True)
    (data / "task_results" / "r.bin").write_bytes(b"x" * 10)
    routes = [route for route in collect_routes(data_dir=data)
              if getattr(route, "path", "") in {"/api/storage", "/api/storage/measure"}]
    assert len(routes) == 2
    before = sorted((str(path), path.stat().st_mtime_ns) for path in data.rglob("*"))
    listings = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda path: listings.append(path) or real(path))

    with TestClient(Starlette(routes=routes)) as client:
        for _ in range(2):
            response = client.get("/api/storage")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert response.json() == {"measuring": False, "measurement": None, "error": None}
        assert listings == [], "a GET must not walk the data folder"

        started = client.post("/api/storage/measure")
        assert started.status_code == 202 and started.json()["started"] is True
        _join_walks()
        assert listings, "the explicit POST runs the walk"

        listings.clear()
        measured = client.get("/api/storage").json()
        assert listings == []
        assert measured["measuring"] is False and measured["error"] is None
        assert _row(measured["measurement"], "results")["bytes"] == 10

    monkeypatch.undo()
    assert sorted((str(path), path.stat().st_mtime_ns) for path in data.rglob("*")) == before


# ------------------------------------------------------------- contract

WEB = Path(__file__).resolve().parent.parent / "web" / "modules"


def test_storage_envelopes_have_a_field_identical_browser_twin():
    """The gateway re-exports the domain-owned envelopes; storage_types.js is their twin."""
    from tests.test_gateway_parity import _js_typedef_fields

    text = (WEB / "storage_types.js").read_text(encoding="utf-8")
    names = [name for name in contracts.__all__ if name.startswith("Storage")]
    assert names == ["StorageCategory", "StorageError", "StorageMeasurement", "StorageResponse"]
    for name in names:
        assert getattr(contracts, name) is getattr(storage, name)
        assert _js_typedef_fields(text, name) == set(getattr(contracts, name).__annotations__), name
    assert "storage_types.js" in (WEB / "api_types.js").read_text(encoding="utf-8").splitlines()[0]
    assert not re.search(r"@typedef \{Object\} Storage", (WEB / "api_types.js").read_text(encoding="utf-8"))
    assert "import('./storage_types.js').StorageResponse" in (WEB / "api_client.js").read_text(encoding="utf-8")


def test_a_real_measurement_has_exactly_the_contract_fields(tmp_path):
    measured = _measure(tmp_path)
    assert set(measured) == set(storage.StorageMeasurement.__annotations__)
    assert all(set(row) == set(storage.StorageCategory.__annotations__) for row in measured["categories"])


def test_the_engine_home_name_is_the_owned_engine_directory():
    from ouroboros.claudexor_daemon import owned_config_dir

    assert owned_config_dir().name == storage.ENGINE_DIR
