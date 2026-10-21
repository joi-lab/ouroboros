"""The cost contract: boot and interactive boundaries do not grow with unrelated history.

BIBLE P5 «Dialogue must not wait for bookkeeping over unrelated history» and P13 «ordinary
enforcement does not enumerate or reconstruct unrelated completed history». Two data roots
(tests/_scale_root.py) hold the SAME current facts; the second holds 30 times the unrelated
history of the first (settled tasks with 20-200 KB bodies, child drives with their own stores,
frozen legacy result files, archive segments of other rooms newer than the requested room's
rows, closed custody records). Each boundary is driven through its real public entry point in a
fresh interpreter (tests/_cost_driver.py) and its work is counted (tests/_cost_meter.py):
connections, statements, SQLite VM steps, file-system calls and bytes read by location, JSON
decodes, child processes and threads. The contract is EQUALITY of those counters between the two
roots; the one allowance is ``VM_ALLOWANCE`` SQLite VM steps for a deeper B-tree (logarithmic in
the history, never a share of it). A full scan hidden inside one SELECT grows the VM steps and
fails; reading the requested rooms' own rows is the same work at every size and passes.

Counting is not timing: this file holds no clock and proves no latency; it makes the next
history-sized reader on these paths fail by name instead of arriving silently.

Where today's code breaks the contract the test is ``xfail(strict=True)`` naming the cause, its
measured numbers and the unit that removes it, so this file is green now and turns red (XPASS)
the moment the fix lands; the marker is then removed. The k=100 variant runs in the opt-in
``integration`` lane (``-m integration -o addopts=''``). ``B6`` is the rest law of the named
maintenance jobs: a second run over unchanged inputs commits nothing and reads no history.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import pathlib
import sqlite3
import sys
from typing import Any, Callable, Dict, Iterable, List

import pytest

from tests._cost_meter import HISTORY, VM_STEP, CostMeter, tagged

pytestmark = pytest.mark.scale

REPO = pathlib.Path(__file__).resolve().parents[1]
K_SMALL = 1
CHILD_TIMEOUT_SEC = 900  # a transport bound for one child, not a judgment about the boot
LOCATIONS = ("root", *HISTORY)
# Counted per location; lock files (``lock``) are left out: how often a lock is probed and retried
# follows the contention between this process's threads at that instant, not the history.
WORK = ("connect", "statements", "begins", "commits", "writes", "json_loads", "json_chars", "popen", "thread_start",
        *(f"{kind}:{where}" for kind in ("connect", "stat", "scandir", "listdir", "open", "os_open", "read_bytes")
          for where in (*LOCATIONS, "outside")))


# Two explicit allowances, both absolute and both far below what one unit of history adds at
# k=30 (every history item exists k times, so a reader that touches one per unit adds at least 29):
# - COUNT_JITTER: the boot runs threads concurrently (store import, supervisor, boot coordinator),
#   so one read of a CURRENT fact can happen twice when one thread misses a cache another is filling;
# - BYTE_JITTER: bytes read and characters decoded differ by the digits of a timestamp or a duration
#   written during the boot itself (9.98 s vs 10.02 s); 4 KiB stays below the smallest history unit
#   here (one 20 KB settled body, one ~25 KB archive segment).
COUNT_JITTER = 2
BYTE_JITTER = 4096
BYTE_KEYS = ("json_chars", *(f"read_bytes:{where}" for where in (*LOCATIONS, "outside")))


def vm_allowance(large: int, small: int = K_SMALL) -> int:
    """SQLite VM steps a deeper index may add (logarithmic in the history, never a share of it), plus the
    progress-handler granularity of the statements ``COUNT_JITTER`` lets differ."""
    return 128 * math.ceil(math.log2(large / small)) + COUNT_JITTER * VM_STEP


# ---- measurement -------------------------------------------------------------------------------

def _child(module: str, args: List[str], env: Dict[str, str], log: pathlib.Path) -> None:
    """One fresh interpreter; a timeout or an interrupt stops its whole process tree (tests/group_run.py)."""
    from tests.group_run import run_in_group

    done = run_in_group([sys.executable, "-m", module, *args], timeout=CHILD_TIMEOUT_SEC, cwd=REPO, env=env)
    log.write_text(done.stdout + done.stderr, encoding="utf-8")
    assert done.returncode == 0, f"{module} exited {done.returncode}:\n{(done.stdout + done.stderr)[-6000:]}"


def measure(base: pathlib.Path, k: int) -> Dict[str, Any]:
    """Build the k-root in one interpreter, then boot and query it in another (cold)."""
    home = base / f"k{k:03d}"
    root, repo = home / "data", home / "repo"
    root.mkdir(parents=True)
    repo.mkdir()
    env = {**os.environ, "OUROBOROS_DATA_DIR": str(root), "OUROBOROS_SETTINGS_PATH": str(root / "settings.json"),
           "OUROBOROS_REPO_DIR": str(repo), "OUROBOROS_HOST_SERVICE_PORT": "0"}
    facts_path, spec_path, out_path = home / "facts.json", home / "spec.json", home / "measured.json"
    _child("tests._scale_root", [str(root), str(k), str(facts_path)], env, home / "build.log")
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    spec_path.write_text(json.dumps({"root": str(root), "facts": facts}), encoding="utf-8")
    _child("tests._cost_driver", [str(spec_path), str(out_path)], env, home / "drive.log")
    measured = json.loads(out_path.read_text(encoding="utf-8"))
    assert not measured["errors"], f"k={k}: the driver failed:\n" + "\n".join(measured["errors"])
    measured["root"] = str(root)
    return measured


@pytest.fixture(scope="module")
def small(tmp_path_factory) -> Dict[str, Any]:
    return measure(tmp_path_factory.mktemp("cost"), K_SMALL)


@pytest.fixture(scope="module", params=[pytest.param(30, id="k30"),
                                        pytest.param(100, id="k100", marks=pytest.mark.integration)])
def measured(request, small, tmp_path_factory) -> Dict[int, Dict[str, Any]]:
    large = request.param
    return {K_SMALL: small, large: measure(tmp_path_factory.mktemp("cost"), large), "large": large}


# ---- the comparison ----------------------------------------------------------------------------

def contract_breaches(small: Dict[str, int], large: Dict[str, int], k_large: int,
                      keys: Iterable[str] = WORK) -> Dict[str, Any]:
    """Every counter that grows from the small root to the large one, and VM steps beyond ``vm_allowance``.
    A count the large root spends less of is background noise in the small root's cold boot, not growth."""
    breaches: Dict[str, Any] = {key: (small.get(key, 0), large.get(key, 0)) for key in keys
                                if large.get(key, 0) - small.get(key, 0) > (
                                    BYTE_JITTER if key in BYTE_KEYS else COUNT_JITTER)}
    allowed = vm_allowance(k_large)
    if large.get("vm_steps", 0) > small.get("vm_steps", 0) + allowed:
        breaches["vm_steps"] = (small.get("vm_steps", 0), large.get("vm_steps", 0), f"allowance {allowed}")
    return breaches


def boundary(measured: Dict[Any, Any], tag: str, k: int) -> Dict[str, Any]:
    entry = measured[k].get(tag)
    assert entry is not None, f"boundary {tag} was not reached at k={k}"
    return entry


def assert_flat(measured: Dict[Any, Any], tag: str) -> None:
    large = measured["large"]
    answers = [{key: value for key, value in (measured[k].get("responses", {}).get(tag) or {}).items()
                if key in ("status", "echo")} for k in (K_SMALL, large)]
    assert answers[0] == answers[1], f"{tag} answered differently at the two sizes: {answers}"  # a failure is not flat
    big = boundary(measured, tag, large)
    little = boundary(measured, tag, K_SMALL)
    breaches = contract_breaches(little["counts"], big["counts"], large)
    touched = sorted(big.get("paths", {}).items(), key=lambda item: -item[1])[:8]
    growing = sorted(((source, amount - little.get("callers", {}).get(source, 0))
                      for source, amount in big.get("callers", {}).items()
                      if amount > little.get("callers", {}).get(source, 0)), key=lambda item: -item[1])[:8]
    assert not breaches, (f"{tag} grows with unrelated history (k={K_SMALL} -> k={large}): {breaches}; "
                          f"history touched at k={large}: {touched}; growing readers: {growing}")


# ---- B1-B5 and the room ------------------------------------------------------------------------

def test_b1_lifespan_before_health_is_flat(measured):
    assert_flat(measured, "B1")
    assert_flat(measured, "B1h")


def test_b1_boot_steps_after_the_yield_are_flat(measured):
    assert_flat(measured, "B1c")


def test_b1_no_child_process_before_health(measured):
    for k in (K_SMALL, measured["large"]):
        assert measured[k]["popen_before_health"] == 0, (k, measured[k]["B1"]["names"])


def test_b2_publication_to_dialogue_is_flat(measured):
    assert_flat(measured, "B2_dialogue")


def test_b2_publication_to_supervisor_ready_is_flat(measured):
    assert_flat(measured, "B2_ready")


def _assert_state_questions(measured, tag):
    """Visible questions stay fixed; k grows delivered history, including the finished author's result."""
    from tests._scale_root import CURRENT

    expected = {"current_questions": sorted([
        {"task_id": CURRENT[key][0], "quiz_id": CURRENT[key][1], "chat_id": 1, "native": True,
         "quiz_state": state} for key, state in (("question", "open"), ("retained_question", "expired_terminal"))
    ], key=lambda row: (row["task_id"], row["quiz_id"])),
        "current_questions_complete": True, "current_questions_gaps": []}
    for k in (K_SMALL, measured["large"]):
        assert measured[k]["responses"][tag]["decisions"] == expected


def test_b3_state_cold_is_flat(measured):
    _assert_state_questions(measured, "B3")
    assert_flat(measured, "B3")


def test_b3_state_warm_is_flat(measured):
    _assert_state_questions(measured, "B3w")
    assert_flat(measured, "B3w")


def test_b4_owner_message_on_a_cold_process_is_flat(measured):
    assert_flat(measured, "B4")
    for k in (K_SMALL, measured["large"]):
        assert boundary(measured, "B4", k)["counts"].get("open:archive", 0) == 0, k


def test_b4_owner_message_on_a_warm_process_is_flat(measured):
    assert_flat(measured, "B4w")


def test_b5_stop_of_a_tree_is_flat(measured):
    assert_flat(measured, "B5")


@pytest.mark.parametrize("tag", ["R_active", pytest.param("R_late", marks=pytest.mark.xfail(strict=True, reason=(
    "The tool-call evidence a late task's card replays (tool_call_log.replay_evidence -> Memory.read_task_recent) "
    "reads up to the three newest tools.jsonl archives, newest first, before it reaches the task's own rows; the "
    "tools log stays a rotated file in this change (k=1 -> k=30: one more tools segment, about 39 KB)."))),
    "R_activew"])
def test_room_history_is_flat(measured, tag):
    for k in (K_SMALL, measured["large"]):  # a page, never the import window's refusal
        assert measured[k]["import"]["state"] == "done" and measured[k]["responses"][tag]["status"] == 200, k
    assert_flat(measured, tag)


# ---- B6: the rest law of the named maintenance jobs ---------------------------------------------

def second_run(run: Callable[[pathlib.Path], Any], root: pathlib.Path) -> Dict[str, int]:
    """The counters of a job's second run over the inputs its first run left."""
    run(root)
    with CostMeter(root) as meter:
        tagged("rest", lambda: run(root))
    return meter.snapshot("rest")["counts"]


def rest_breaches(counts: Dict[str, int]) -> Dict[str, int]:
    """A second run writes nothing (a read transaction of the store also ends with COMMIT, so the
    written statements are what counts) and opens or reads nothing of the history. Looking whether a
    history directory is empty is allowed; that it does not grow is the k-equality's to check."""
    keys = ("writes", *(f"{kind}:{where}" for kind in ("open", "os_open", "read_bytes") for where in HISTORY))
    return {key: counts[key] for key in keys if counts.get(key)}


def _maintenance():
    try:
        from ouroboros import maintenance
    except ImportError:
        return None
    return maintenance


# Every job the maintenance child runs: its own registry.
JOB_NAMES = tuple(getattr(_maintenance(), "JOBS", {}))


def job_runner(name: str) -> Callable[[pathlib.Path], Any]:
    """One named job's run in this process: ``maintenance.run_job(root, name)``."""
    from tests._store_shared import as_install

    maintenance = _maintenance()

    def run(root: pathlib.Path) -> Any:
        with as_install(root):  # the maintenance child runs as the install it maintains
            return maintenance.run_job(root, name, should_stop=lambda: False)
    return run


def _rests_at_every_size(measured: Dict[Any, Any], run: Callable[[pathlib.Path], Any], what: str) -> None:
    large = measured["large"]
    rested = {k: second_run(run, pathlib.Path(measured[k]["root"])) for k in (K_SMALL, large)}
    for k, counts in rested.items():
        assert not rest_breaches(counts), (what, k, rest_breaches(counts))
    assert not contract_breaches(rested[K_SMALL], rested[large], large), (what, rested)


@pytest.mark.parametrize("name", JOB_NAMES)
def test_b6_each_job_rests_on_its_second_run(measured, name):
    _rests_at_every_size(measured, job_runner(name), name)


def test_b6_the_maintenance_pass_rests_on_its_second_run(measured):
    maintenance = _maintenance()
    if maintenance is None or not callable(getattr(maintenance, "run", None)):
        pytest.skip("the maintenance child's pass does not exist on this tree yet")
    _rests_at_every_size(measured, lambda root: maintenance.run(root, should_stop=lambda: False), "maintenance.run")


# ---- the instrument itself, both directions --------------------------------------------------------

def _rooms(root: pathlib.Path, rows: int) -> pathlib.Path:
    db = root / "state" / "current.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, room INTEGER, body TEXT)")
    conn.execute("CREATE INDEX rows_room ON rows(room)")
    conn.executemany("INSERT INTO rows (room, body) VALUES (?, ?)",
                     [(7 if index < 5 else 1000 + index, "x" * 40) for index in range(rows)])
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize("hidden_scan", [False, True])
def test_the_contract_catches_a_scan_hidden_in_one_select(tmp_path, hidden_scan):
    """The same requested rows read through an index stay flat; read through a predicate no index serves,
    the one SELECT walks the whole table and the VM steps grow with it."""
    query = ("SELECT body FROM rows WHERE room + 0 = 7" if hidden_scan else "SELECT body FROM rows WHERE room = 7")
    counted = {}
    for k in (K_SMALL, 30):
        db = _rooms(tmp_path / f"k{k:03d}", 200 * k)
        with CostMeter(tmp_path / f"k{k:03d}") as meter:
            def read():
                conn = sqlite3.connect(db)
                assert len(conn.execute(query).fetchall()) == 5
                conn.close()
            tagged("read", read)
        counted[k] = meter.snapshot("read")["counts"]
    assert counted[K_SMALL]["statements"] == counted[30]["statements"] == 1
    assert bool(contract_breaches(counted[K_SMALL], counted[30], 30)) is hidden_scan, counted


@pytest.mark.parametrize("walks_history", [False, True])
def test_the_contract_catches_a_history_walk(tmp_path, walks_history):
    """A reader that opens only the requested segment is flat; one that walks every segment is not."""
    counted = {}
    for k in (K_SMALL, 30):
        archive = tmp_path / f"k{k:03d}" / "archive"
        archive.mkdir(parents=True)
        for index in range(k):
            (archive / f"chat_{index:04d}.jsonl").write_text('{"chat_id": 9}\n' * 20, encoding="utf-8")
        (archive / "chat_0000.jsonl").write_text('{"chat_id": 7}\n' * 20, encoding="utf-8")

        def read(archive=archive):
            names = sorted(os.listdir(archive)) if walks_history else ["chat_0000.jsonl"]
            for name in names:
                with open(archive / name, encoding="utf-8") as handle:
                    [json.loads(line) for line in handle]

        with CostMeter(tmp_path / f"k{k:03d}") as meter:
            tagged("read", read)
        counted[k] = meter.snapshot("read")["counts"]
    assert bool(contract_breaches(counted[K_SMALL], counted[30], 30)) is walks_history, counted


@pytest.mark.parametrize("rests", [False, True])
def test_the_rest_law_catches_a_job_that_redoes_its_work(tmp_path, rests):
    """A job that records its result reads nothing the second time; one that forgets re-reads the archive."""
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "chat_0001.jsonl").write_text('{"chat_id": 9}\n' * 20, encoding="utf-8")
    done = tmp_path / "state" / "job.done"
    done.parent.mkdir()

    def job(root: pathlib.Path) -> None:
        if rests and done.exists():
            return
        with open(root / "archive" / "chat_0001.jsonl", encoding="utf-8") as handle:
            [json.loads(line) for line in handle]
        done.write_text("done", encoding="utf-8")

    assert bool(rest_breaches(second_run(job, tmp_path))) is not rests


@pytest.fixture
def contained_meter():
    """The meter tests below make installs and uninstalls fail on purpose. A regression could leave a
    meter installed; it is put back when the test ends, so the failure stays this test's own instead of
    cascading into every later meter test as "one cost meter at a time"."""
    from tests import _cost_meter

    yield
    if _cost_meter._ACTIVE is not None:
        with contextlib.suppress(Exception):  # the test's own failure is the one to report
            _cost_meter.uninstall(_cost_meter._ACTIVE)


def test_the_meter_installs_where_a_door_is_missing_and_never_records_a_wrapper_as_the_original(
        tmp_path, monkeypatch, contained_meter):
    """``os.pread`` is POSIX-only, so on Windows the meter counts without it. An install that meets a door still
    wrapped refuses and puts back every door it replaced: a wrapper recorded as the original would recurse in
    every later caller of that door, in this process and in every test after it."""
    from tests import _cost_meter

    real_stat, real_open = os.stat, open
    with monkeypatch.context() as patch:
        patch.setattr(json, "loads", _cost_meter._w_json_loads)  # left wrapped; os.stat .. open come first
        with pytest.raises(RuntimeError, match="still the meter's wrapper"):
            CostMeter(tmp_path).__enter__()
        assert os.stat is real_stat and open is real_open
    monkeypatch.delattr(os, "pread", raising=False)
    with CostMeter(tmp_path) as meter:
        tagged("read", lambda: os.stat(tmp_path))
    assert meter.snapshot("read")["counts"]["stat"] == 1 and os.stat is real_stat


@pytest.mark.parametrize("takes", [0, 1], ids=["rebinding_refused", "restoring_refused"])
def test_the_meter_puts_every_door_back_past_a_module_that_refuses(tmp_path, monkeypatch, takes, contained_meter):
    """A module holding a door's alias may refuse the wrapper (the install fails) or refuse to give it
    back (the uninstall fails). Either way every other door goes back, the refusal is named once,
    and the next install starts from the real doors."""
    import types

    class OneWay(types.ModuleType):
        """Takes ``takes`` assignments of its ``stat`` alias, then refuses."""

        def __setattr__(self, attr, value):
            if attr == "stat":
                if not self.__dict__["takes"]:
                    raise TypeError("stat is read-only here")
                self.__dict__["takes"] -= 1
            super().__setattr__(attr, value)

    alias = OneWay("tests._cost_meter_one_way")
    alias.__dict__.update(stat=os.stat, takes=takes)
    monkeypatch.setitem(sys.modules, alias.__name__, alias)
    real_stat, real_open = os.stat, open
    if takes:
        meter = CostMeter(tmp_path).__enter__()
        assert alias.stat is not real_stat, "the alias took the wrapper"
        with pytest.raises(RuntimeError, match=r"could not put back tests\._cost_meter_one_way\.stat"):
            meter.__exit__(None, None, None)
    else:
        with pytest.raises(TypeError, match="read-only"):
            CostMeter(tmp_path).__enter__()
    assert os.stat is real_stat and open is real_open, "every other door went back"
    monkeypatch.delitem(sys.modules, alias.__name__)
    with CostMeter(tmp_path) as meter:
        tagged("read", lambda: os.stat(tmp_path))
    assert meter.snapshot("read")["counts"]["stat"] == 1
