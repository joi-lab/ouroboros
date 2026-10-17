"""Settlement owes every obligation set but the declared records, an undeclared stored one included; the
census reads the store once and closes it before any directory probe; new packs put small members first."""
from __future__ import annotations

import ast
import hashlib
import json
import lzma
import pathlib
import re
import sqlite3
import threading

import pytest

from ouroboros import evidence_packer as packer, evidence_packs as packs, obligations, store
from tests.test_evidence_packs import _blob, _handle_path, _loose, _result, _task
from tests.test_evidence_packs import root as root

REPO = pathlib.Path(__file__).resolve().parents[1]
_TXN_SET_ARG = {"put_row", "set_rows", "delete_row", "get_row", "replace_set"}  # store.Txn: the set name first
_MODULE_SET_ARG = {"add", "remove", "members", "members_in", "result_rows"}  # obligations: (root|txn, name, ...)
_MARKER = re.compile(r"\.(?:put_row|set_rows|delete_row|get_row|replace_set)\(|^\s*(?:from|import)\s.*\bobligations\b", re.M)
# These pass a caller's set name through or copy every set (import/export); their callers are enumerated.
_FORWARDERS = {("ouroboros/obligations.py", name) for name in ("members_in", "members", "add", "remove", "result_rows")} | {
    ("ouroboros/evidence_packs.py", "settlement_obligations"), ("ouroboros/store_import.py", "_reconcile_sets"),
    ("ouroboros/store_import.py", "_build"), ("ouroboros/store_import.py", "export_facts")}


def _own(scope):
    """The scope's nodes, nested function bodies excluded (each is its own scope)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(ast.iter_child_nodes(node))


def _constants(tree) -> dict:
    return {node.targets[0].id: node.value.value for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}


def _set_name_sites(sources: dict, constants_of):
    """Every set name an obligations call site reaches (literals, module constants, imported constants,
    loop tuples, f-strings over them); unresolvable sites outside the declared forwarders."""
    names, unresolved, forwarders = {}, [], set()
    for rel, tree in sources.items():
        own, modules, functions, imported = _constants(tree), set(), set(_MODULE_SET_ARG) if rel == "ouroboros/obligations.py" else set(), {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ouroboros":
                modules |= {a.asname or a.name for a in node.names if a.name == "obligations"}
            elif isinstance(node, ast.ImportFrom) and node.module == "ouroboros.obligations":
                functions |= {a.asname or a.name for a in node.names if a.name in _MODULE_SET_ARG}
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("ouroboros."):
                source = constants_of(node.module.replace(".", "/") + ".py")
                imported.update({a.asname or a.name: source[a.name] for a in node.names if a.name in source})
        for scope in [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))]:
            loops = {node.target.id: [e.value for e in node.iter.elts] for node in _own(scope)
                     if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and isinstance(node.iter, (ast.Tuple, ast.List))
                     and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.iter.elts)}

            def resolve(arg):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    return [arg.value]
                if isinstance(arg, ast.Name):
                    found = loops.get(arg.id) or ([own[arg.id]] if arg.id in own else None)
                    return found or ([imported[arg.id]] if arg.id in imported else None)
                if isinstance(arg, ast.JoinedStr):
                    out = [""]
                    for part in arg.values:
                        pieces = [part.value] if isinstance(part, ast.Constant) else resolve(part.value)
                        if pieces is None:
                            return None
                        out = [head + piece for head in out for piece in pieces]
                    return out
                return None

            for call in (n for n in _own(scope) if isinstance(n, ast.Call)):
                func, index = call.func, None
                if isinstance(func, ast.Attribute) and func.attr in _TXN_SET_ARG:
                    index = 0
                elif (isinstance(func, ast.Attribute) and func.attr in _MODULE_SET_ARG and isinstance(func.value, ast.Name)
                      and func.value.id in modules) or (isinstance(func, ast.Name) and func.id in functions):
                    index = 1
                if index is None:
                    continue
                resolved = resolve(call.args[index]) if len(call.args) > index else None
                if resolved is not None:
                    for name in resolved:
                        names.setdefault(name, set()).add(f"{rel}:{call.lineno}")
                elif (rel, getattr(scope, "name", "")) in _FORWARDERS:
                    forwarders.add((rel, scope.name))
                else:
                    unresolved.append(f"{rel}:{call.lineno} {ast.unparse(call)[:80]}")
    return names, unresolved, forwarders


def _repo_sources():
    cache = {}

    def constants_of(rel):
        if rel not in cache:
            path = REPO / rel
            cache[rel] = _constants(ast.parse(path.read_text(encoding="utf-8"))) if path.is_file() else {}
        return cache[rel]

    sources = {}
    for path in [REPO / "server.py", *sorted((REPO / "ouroboros").rglob("*.py")), *sorted((REPO / "supervisor").rglob("*.py"))]:
        text = path.read_text(encoding="utf-8")
        if _MARKER.search(text):
            sources[path.relative_to(REPO).as_posix()] = ast.parse(text)
    return sources, constants_of


def _unclassified(names) -> set:
    return {name for name in names if (name in obligations.DUTY_SETS) == (name in obligations.RECORD_SETS)}


def test_every_set_name_a_writer_can_reach_is_exactly_one_duty_or_record():
    """A record stops blocking only once named, so every name this body's writers reach is declared."""
    assert not set(obligations.DUTY_SETS) & set(obligations.RECORD_SETS)
    assert set(store.FLAG_COLUMNS) <= set(obligations.DUTY_SETS)
    names, unresolved, forwarders = _set_name_sites(*_repo_sources())
    assert unresolved == [], "resolve the set name or declare the forwarder"
    assert forwarders == _FORWARDERS  # the allowance never outlives its code
    # Constant-routed (WITNESS_SET) and generated (f"custody_{name}") names are reached, not only literals.
    assert {"local_custody_witnesses", "upgrade_notices", "pause_notice_receipts",
            "custody_marks", "custody_refused", "custody_open", "pending_drives", "unknowns"} <= set(names)
    assert _unclassified(set(names) | set(obligations.BOOT_SETS)) == set()
    # The guard fires: a new set reached through a literal or a constant is unclassified.
    probe = {"ouroboros/new_owner.py": ast.parse(
        "from ouroboros import obligations\nNEW = 'fresh_constant_set'\n"
        "def write(root):\n    obligations.add(root, 'fresh_literal_set', 'id')\n    obligations.add(root, NEW, 'id')\n")}
    probe_names, probe_unresolved, _ = _set_name_sites(probe, lambda rel: {})
    assert _unclassified(probe_names) == {"fresh_literal_set", "fresh_constant_set"} and probe_unresolved == []


def test_every_set_the_real_producers_store_is_classified(root):
    from contextlib import closing
    from ouroboros import delegate_custody as custody, delegate_custody_current as current, local_custody_repair
    from ouroboros import notice_receipts
    from tests.test_custody_fold_equivalence import _chain

    rows = [*_chain(), {"type": "delegate_run_start_blocked", "event_id": "review", "task_id": "t6", "source": "review_substrate"}]
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as conn:  # the live custody reducer
        conn.row_factory = sqlite3.Row
        conn.executescript(store._SCHEMA)
        txn = store.Txn(conn)
        for row in custody._unique_rows(rows):
            current._reduce(txn, row, current._loaded(txn, row), {})
        names = set(txn.set_names())
        # A review-owned attempt marks nothing: neither the live reducer nor the import fold writes an empty row.
        assert txn.get_row("custody_marks", "t1") and txn.get_row("custody_marks", "t6") is None
        assert "t6" not in current.fold(rows)["marks"]
    _result(root, "t")
    notice_receipts.record(root, {"direction": "system", "type": sorted(notice_receipts.UPGRADE_TYPES)[0], "chat_id": 1})
    notice_receipts.record(root, {"direction": "system", "type": notice_receipts.PAUSE_TYPE, "chat_id": 1,
                                  "card_row_id": "card", "task_id": "t"})
    assert local_custody_repair.retain_witness(root, "t", reason="test", model_consumers={"c": 1})
    obligations.drive_started(root, {"id": "t"})
    with store.open_store(root, write=False) as txn:
        names |= set(txn.set_names())
    assert {"custody_open", "custody_refused", "custody_marks", "delegated_runs", "upgrade_notices",
            "pause_notice_receipts", "local_custody_witnesses", "pending_drives"} <= names
    assert _unclassified(names) == set()


@pytest.mark.parametrize("record", obligations.RECORD_SETS)
def test_a_record_row_never_blocks_settlement_but_a_duty_row_does(root, record):
    refs, _ = _task(root, "done", blobs=1)
    obligations.add(root, record, "done", {"task_id": "done", "request": {"task_id": "done"}})
    assert packs.task_settled(root, "done")
    assert packer.run_pass(root)["packed"] == 1 and not pathlib.Path(refs[0]["path"]).exists()
    refs, _ = _task(root, "owed", blobs=1)
    obligations.add(root, "custody_open", "invocation:owed", {"request": {"task_id": "owed"}})
    obligations.add(root, record, "owed", {"task_id": "owed"})
    assert not packs.task_settled(root, "owed") and packer.run_pass(root)["packed"] == 0
    assert pathlib.Path(refs[0]["path"]).exists()


UNDECLARED = "future_body_duty"  # declared in neither tuple: an older or newer body's own duty


def _undeclared_duty(root, writer, task_id):
    """One duty row this body never declared, owned by TASK_ID, through a real writer; its identity."""
    identity = f"work:{task_id}"
    if writer == "add":  # the owner API
        obligations.add(root, UNDECLARED, identity, {"task_id": task_id})
    elif writer == "direct":  # a transaction's own row write, as the custody and receipt owners do
        with store.open_store(root, write=True) as txn:
            txn.put_row(UNDECLARED, identity, {"request": {"task_id": task_id}})
    else:  # an older body's set file, read by the boot import that publishes the store
        from ouroboros.store_import import ensure_current_store, set_file_path
        from ouroboros.utils import atomic_write_json

        atomic_write_json(set_file_path(root, UNDECLARED), {identity: {"task_id": task_id}})
        assert ensure_current_store(root, with_children=False)["status"] == "completed"
    return identity


@pytest.mark.parametrize("writer", ["add", "direct", "import"])
def test_an_undeclared_stored_duty_holds_its_evidence_until_retired(root, writer):
    """An unknown duty never passes for a record: its owner stays unsettled, the pass packs only the
    neighbour and every loose byte reads back unchanged; once the row is retired the next pass packs
    it and the same bytes come from the pack. Declared records on the same task never block."""
    identity = _undeclared_duty(root, writer, "owed") if writer == "import" else None
    refs, handle = _task(root, "owed", blobs=2)
    _task(root, "free", blobs=1)
    identity = identity or _undeclared_duty(root, writer, "owed")
    for record in obligations.RECORD_SETS:
        obligations.add(root, record, "owed", {"task_id": "owed"})
    with store.open_store(root, write=False) as txn:
        assert UNDECLARED in txn.set_names() and UNDECLARED not in obligations.DUTY_SETS + obligations.RECORD_SETS
    raws, handle_raw = [packs.blob_bytes(root, ref) for ref in refs], packs.handle_bytes(root, "owed", handle["path"])
    assert not packs.task_settled(root, "owed") and packs.task_settled(root, "free")
    first = packer.run_pass(root)
    assert (first["packed"], first["unlinked"]) == (1, 2) and not packs.published_generations(root, "owed")
    assert all(_loose(ref) for ref in refs) and _handle_path(root, "owed", handle).exists()
    assert [packs.blob_bytes(root, ref) for ref in refs] == raws
    assert packer.changed_tasks(root) == ["free"]  # its own unlinks moved its directory; the owed task rests
    packer.run_pass(root)  # that one look
    obligations.remove(root, UNDECLARED, identity)
    assert packs.task_settled(root, "owed") and packer.changed_tasks(root) == ["owed"]
    second = packer.run_pass(root)
    assert (second["packed"], second["unlinked"]) == (1, 3) and not any(_loose(ref) for ref in refs)
    assert [packs.blob_bytes(root, ref) for ref in refs] == raws
    assert packs.handle_bytes(root, "owed", handle["path"]) == handle_raw


@pytest.mark.parametrize("writer", ["add", "direct"])
def test_an_undeclared_duty_arriving_before_the_unlink_keeps_every_loose_file(root, monkeypatch, writer):
    """The duty lands after the census and the pack write, before the unlink section: the section's
    recheck sees it, nothing loose goes, and the duplicated bytes read the same from both places."""
    refs, handle = _task(root, "late", blobs=2)
    raws, verify, arrived = [packs.blob_bytes(root, ref) for ref in refs], packs.verify_pack, []

    def arrive(*args, **kwargs):
        if not arrived:
            arrived.append(_undeclared_duty(root, writer, "late"))
        return verify(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(packs, "verify_pack", arrive)
        report = packer.run_pass(root)
    assert arrived and (report["packed"], report["unlinked"]) == (1, 0)
    assert all(_loose(ref) for ref in refs) and _handle_path(root, "late", handle).exists()
    assert [packs.blob_bytes(root, ref) for ref in refs] == raws
    obligations.remove(root, UNDECLARED, arrived[0])
    assert packer.run_pass(root)["unlinked"] == 3 and not any(_loose(ref) for ref in refs)
    assert [packs.blob_bytes(root, ref) for ref in refs] == raws


def test_census_reads_the_store_once_for_every_loose_task(root, monkeypatch):
    for number in range(24):
        _task(root, f"t{number}", blobs=1, status="running" if number % 2 else "completed")
    opened, original = [], store.connect

    def connect(path, *args, **kwargs):
        if pathlib.Path(path) == store.store_path(root):
            opened.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(store, "connect", connect)
    assert len(packer.changed_tasks(root)) == 24
    assert len(opened) == 1  # one short read for every row and duty, not one per task
    with packer.pack_sweep(root) as sweep:
        opened.clear()
        assert len(packer.changed_stamps(root, obligation_rows=sweep.duties, external=sweep.external, seqs=sweep.seqs)) == 24
    assert opened == []  # the child census reuses the sweep's baseline read


@pytest.mark.parametrize("operation", ["changed_tasks", "run_pass"])
@pytest.mark.parametrize("tier", [store.TIER_ENFORCED, store.TIER_NAME])
def test_a_writer_commits_while_a_census_directory_probe_stalls(root, monkeypatch, operation, tier):
    """No store transaction spans the census's ``stat`` calls. On the enforced tier a held read lock
    (a LockFileEx range on Windows) delays the writer's COMMIT; on the name tier the reader holds the
    one name lock; either way the 1 s writer below would fail with StoreBusy. The write lands on the
    stalled task itself: the stamp the census returns predates it, so the task stays selectable."""
    from tests.test_store import publish

    publish(root, tier=tier)
    _task(root, "stalled", blobs=1)
    _task(root, "other", blobs=1)
    target = root / "observability" / "calls" / "stalled"
    entered, release, result = threading.Event(), threading.Event(), {}
    original = pathlib.Path.stat

    def stat(self, *args, **kwargs):
        if self == target and threading.current_thread().name == "census" and not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original(self, *args, **kwargs)

    def census():
        result["value"] = packer.changed_stamps(root) if operation == "changed_tasks" else packer.run_pass(root)

    monkeypatch.setattr(pathlib.Path, "stat", stat)
    worker = threading.Thread(target=census, name="census")
    worker.start()
    try:
        assert entered.wait(10)
        with store.open_store(root, write=True, wait_sec=1.0) as txn:
            txn.store_task({**txn.load("stalled"), "updated_at": "2026-10-12T00:00:00+00:00"})
    finally:
        release.set()
        worker.join(30)
    assert not worker.is_alive()
    if operation == "changed_tasks":
        assert set(result["value"]) == {"stalled", "other"}
        for task, stamp in result["value"].items():
            packer.record_swept(root, task, stamp)
        assert packer.changed_tasks(root) == ["stalled"]
    else:
        assert result["value"]["faults"] == 0 and "stalled" in packer.changed_tasks(root)


def test_new_packs_order_checkpoints_then_blobs_smallest_first_and_old_order_stays_readable(root):
    payloads = [b"L" * 9000, b"s", b"M" * 300, b"t", b"m" * 300]
    refs, handle = _task(root, "ordered", blobs=len(payloads), payloads=payloads)
    assert packer.run_pass(root)["packed"] == 1
    sidecar = packs.read_sidecar(packs.packs_dir(root) / "ordered.members.json")
    members = sorted(sidecar["members"], key=lambda m: m["offset"])
    assert members[0]["kind"] == "checkpoint" and members[0]["path"] == handle["path"]
    blobs = [(m["size"], m["sha256"]) for m in members[1:]]
    assert blobs == sorted(blobs) and blobs != [(len(p), hashlib.sha256(p).hexdigest()) for p in payloads]
    assert [packs.blob_bytes(root, ref) for ref in refs] == payloads
    assert packs.handle_bytes(root, "ordered", handle["path"]).startswith(b"# focus")
    # A pack written before the order change (blobs in manifest order, large first) still resolves.
    raws = (b"Z" * 5000, b"a")
    old, raw_total = [_blob(root, raw) for raw in raws], sum(len(raw) for raw in raws)
    dict_size = packs.dict_size_for(raw_total)
    pack = lzma.compress(b"".join(raws), format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64,
                         filters=[{"id": lzma.FILTER_LZMA2, "preset": packs.PRESET, "dict_size": dict_size}])
    (packs.packs_dir(root) / "legacy.xz").write_bytes(pack)
    (packs.packs_dir(root) / "legacy.members.json").write_text(json.dumps({
        "task_id": "legacy", "generation": 1, "format_version": 1, "codec": "xz", "preset": packs.PRESET,
        "dict_size": dict_size, "raw_total": raw_total, "packed_total": len(pack),
        "pack_sha256": hashlib.sha256(pack).hexdigest(), "created_at": "2026-10-01T00:00:00Z",
        "members": [{"sha256": ref["sha256"], "kind": "json", "size": ref["size"], "offset": offset}
                    for ref, offset in zip(old, (0, len(raws[0])))]}), encoding="utf-8")
    for ref in old:
        pathlib.Path(ref["path"]).unlink()
    (packs.packs_dir(root) / packs.INDEX_NAME).unlink()
    packs._VALIDATED.clear()
    assert [packs.blob_bytes(root, ref) for ref in old] == list(raws)
