"""Host-written pytest inventory for selected gate attempts (never receipt authority)."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import uuid


MODULE = "ouroboros_selection_inventory"
# Loaded explicitly by each controller; worker hooks forward their collection via
# xdist. No candidate module imports or persisted selection manifest are needed.
SOURCE = '''import json
from pathlib import Path
import pytest

_path = Path(__RESULT_PATH__)
_worker = False

def _emit(event, **fields):
    if not _worker:
        with _path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(event=event, **fields)) + "\\n")

def pytest_configure(config):
    global _worker
    _worker = hasattr(config, "workerinput")
    _emit("start")

def pytest_collection_finish(session):
    if not _worker and session.items:
        _emit("collection", ids=[item.nodeid for item in session.items])

@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    _emit("collection", ids=list(ids))

def pytest_runtest_logreport(report):
    if report.when == "teardown" or report.skipped or report.failed:
        _emit("report", nodeid=report.nodeid, when=report.when, outcome=report.outcome)

@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    yield
    _emit("finish", exitcode=int(session.exitstatus))
'''


def selection_passes(selection, *, probe_module="", temp_root=None, inventory_dir=None):
    """Probe modules live in scratch; ``inventory_dir`` (default temp_root) keeps the full JSONL."""
    from ouroboros import preflight_runner as pr

    passes = []
    for spec in pr._preflight_pass_specs(probe_module=probe_module):
        module = MODULE
        if temp_root is not None:
            module += "_" + uuid.uuid4().hex
            probe = pr._probe_dir(Path(temp_root))
            probe.mkdir(parents=True, exist_ok=True)
            result = Path(inventory_dir or temp_root) / f"inventory-{spec.label}.jsonl"
            result.write_text("", encoding="utf-8")
            (probe / f"{module}.py").write_text(SOURCE.replace("__RESULT_PATH__", repr(str(result))), encoding="utf-8")
        args = [arg for arg in spec.args if arg != "tests/"]
        passes.append(pr._make_pass(spec.label, [*selection.targets, *args, "-p", module]))
    return passes


def read_inventory(temp_root, label):
    try:
        rows = [json.loads(line) for line in
                (Path(temp_root) / f"inventory-{label}.jsonl").read_text(encoding="utf-8").splitlines()]
        collections = [row["ids"] for row in rows if row["event"] == "collection"]
        reports = [row for row in rows if row["event"] == "report"]
        result = {"expected": collections[0] if collections else [], "collections": collections,
                  "executed": [r["nodeid"] for r in reports if r["when"] == "teardown"],
                  "skipped": [r["nodeid"] for r in reports if r["outcome"] == "skipped"],
                  "failed": [r["nodeid"] for r in reports if r["outcome"] == "failed"],
                  "exitcode": rows[-1].get("exitcode") if rows else None}
        expected, executed = result["expected"], result["executed"]
        if not all(isinstance(value, list) and all(isinstance(item, str) for item in value)
                   for value in (expected, executed, result["skipped"], result["failed"])):
            raise ValueError("invalid node inventory")
        problem = ""
        if any(collection != expected for collection in result["collections"]):
            problem = "worker collections disagree"
        elif len(set(expected)) != len(expected) or Counter(executed) != Counter(expected):
            problem = "expected and executed test ids differ (missing or duplicate execution)"
        elif result["exitcode"] not in (0, 5) or result["failed"]:
            problem = "test session did not finish green"
        result.pop("collections")
        return result, problem
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {}, f"test inventory unavailable: {exc}"


_LISTED_IDS = 50  # bounded excerpt of ids worth reading; counts and the digest cover the rest


def compact_inventory(inventory):
    """Counts, a digest of the expected ids and only the ids worth reading.

    The complete inventory stays with the run (and in the CLI's selection.json); a
    durable record repeated in events, exhibits, ledgers and handoffs never carries it.
    """
    compact = {}
    for label, row in (inventory or {}).items():
        expected, executed = row.get("expected", []), row.get("executed", [])
        compact[label] = {
            "expected": len(expected), "executed": len(executed), "skipped": len(row.get("skipped", [])),
            "digest": hashlib.sha256("\n".join(sorted(expected)).encode("utf-8")).hexdigest(),
            "failed": row.get("failed", [])[:_LISTED_IDS],
            "missing": sorted((Counter(expected) - Counter(executed)).elements())[:_LISTED_IDS],
            "unexpected": sorted((Counter(executed) - Counter(expected)).elements())[:_LISTED_IDS],
            "exitcode": row.get("exitcode"),
        }
    return compact


def scope_record(proof, inventory=None):
    selection = getattr(proof, "selection", None)
    rule = "full default suite" if proof.tier == "full" else "custom pytest selection"
    return {"tier": proof.tier, **(selection.record() if selection else {"rule": rule}),
            "candidate": proof.tree, "head": proof.head, "index_tree": proof.index_tree,
            **({"inventory": compact_inventory(inventory)} if inventory is not None else {})}


def inventory_problem(selection, inventory):
    """Every floor module must collect; opt-in-only affected modules may be excluded."""
    from ouroboros.test_selection import FLOOR_TESTS

    collected = {node.split("::", 1)[0] for row in inventory.values() for node in row.get("expected", [])}
    missing = sorted(set(FLOOR_TESTS) - collected)
    return "floor modules collected no tests: " + ", ".join(missing) if missing else ""


def record_failed_attempt(ctx, selection, subject, inventory):
    """Keep partial execution visible without granting it any proof authority."""
    if ctx is None or selection is None or getattr(ctx, "_preflight_tests_passed", False):
        return
    from ouroboros.preflight_receipts import canonical_data_root
    from ouroboros.utils import append_jsonl, utc_now_iso

    event = {"type": "preflight_test_attempt", "ts": utc_now_iso(), "status": "failed",
             **(scope_record(subject, inventory) if subject else selection.record())}
    ctx._preflight_test_disclosure = event
    try:
        root = canonical_data_root(ctx)
        if root:
            append_jsonl(Path(root) / "logs/events.jsonl", event)
    except Exception:
        pass  # Diagnostics cannot replace the actual failure.
