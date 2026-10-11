"""Opt-in, safe CI result facts; raw JUnit is deliberately never read or uploaded.

Registered after tests/conftest.py establishes isolation. The summary entrypoint
uses only stdlib and trusts the supplied Actions producer outcome, not case totals.
events.jsonl is the controller's incremental journal: it names the tests in flight
when a session is killed before its final results.json export.

Everything here is diagnostic except `reconcile-shards`: the sharded UI browser lane
is proven complete only by its shards' projections, so there a missing or invalid
projection is a failure, never an unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter
import html
import json
import math
import os
from pathlib import Path
import platform
import sys
import tempfile

_PHASES = frozenset({"setup", "call", "teardown"})


def output_dir(config) -> Path | None:
    value = getattr(getattr(config, "option", None), "ci_evidence_dir", None)
    return Path(value).resolve() if value else None


def write_json(path: Path, value) -> None:
    """Write an already approved public projection, never a private source blob."""
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cell(value) -> str:
    return html.escape(str(value), quote=True).replace("|", "&#124;").replace("\n", "<br>")


def _case_outcomes(reports: list[dict]) -> dict[str, str]:
    cases = {}
    for row in reports:
        node = row["nodeid"]
        status = row["outcome"]
        if status == "failed":
            cases[node] = "failed"
        elif cases.get(node) != "failed":
            if status == "skipped":
                cases[node] = "skipped"
            elif row["phase"] == "call":
                if cases.get(node) != "skipped":
                    cases[node] = "passed"
            else:
                cases.setdefault(node, "not_run")
    return cases


def _valid(row) -> bool:
    return (isinstance(row, dict)
            and all(isinstance(row.get(key), str) for key in ("nodeid", "phase", "outcome"))
            and (row["phase"] in _PHASES or row["phase"] == "crash")
            and row["outcome"] in {"passed", "failed", "skipped"})


def _final(root: Path) -> tuple[dict, str]:
    """The session's final projection, or the reason it cannot be trusted."""
    try:
        data = json.loads((Path(root) / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, "result projection unavailable"
    reports = data.get("reports") if isinstance(data, dict) else None
    if isinstance(reports, list) and all(map(_valid, reports)):
        return data, ""
    return {}, "invalid result projection"


def _journal(root: Path) -> tuple[list[str], list[dict]] | None:
    """Tests in flight and failed reports from a session without a final export.

    A kill can tear the last line; unparsable and invalid rows are skipped.
    """
    try:
        text = (Path(root) / "events.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    running, failed = {}, []
    for line in text.split("\n"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("nodeid"), str):
            continue
        if row.get("event") == "start":
            running[row["nodeid"]] = None
        elif row.get("event") == "finish":
            running.pop(row["nodeid"], None)
        elif row.get("event") == "report" and _valid(row):
            if row["phase"] == "crash":
                running.pop(row["nodeid"], None)  # A crashed worker never sends finish.
            if row["outcome"] == "failed":
                failed.append(row)
    return list(running), failed


def _flight_table(running: list[str]) -> list[str]:
    if not running:
        return []
    return ["", "| Test in flight when the session ended |", "| --- |"] + [
        f"| {_cell(node)} |" for node in running]


def _failure_table(failed: list[dict]) -> list[str]:
    if not failed:
        return []
    return ["", "| Test | Phase | Error type |", "| --- | --- | --- |"] + [
        f"| {_cell(row['nodeid'])} | {_cell(row['phase'])} | "
        f"{_cell(row.get('error_type') or 'see test step')} |" for row in failed]


def render_summary(root: Path, *, producer_outcome: str, artifact_outcome: str,
                   artifact_url: str = "", scope: str = "full", label: str = "") -> str:
    """Reporter failure cannot replace producer truth; missing reports stay unknown."""
    data, error = _final(root)
    available = not error
    lines = [
        "## CI test evidence" + (f" — {_cell(label)}" if label else "")
        + (" — PARTIAL DIAGNOSTIC" if scope != "full" else ""),
        "",
        f"Producer step: **{_cell(producer_outcome)}**. Selection: **{_cell(scope)}**.",
        "Testcase results and diagnostic availability are separate from that process outcome.",
        "",
    ]
    if available:
        identity = data.get("github", {})
        if identity:
            lines.append(f"Commit: `{_cell(identity.get('sha', 'unknown'))}`; "
                         f"run {_cell(identity.get('run_id', 'unknown'))}, "
                         f"attempt {_cell(identity.get('run_attempt', 'unknown'))}.")
        counts = Counter(_case_outcomes(data["reports"]).values())
        lines.append("Cases: " + ", ".join(f"{name}={counts[name]}" for name in
                                         ("passed", "failed", "skipped", "not_run")) + ".")
        lines.append(f"Observed pytest session exit: {_cell(data.get('session_exit_code', 'unknown'))}.")
        collected = data.get("tests_collected")
        if isinstance(collected, int) and collected > sum(counts.values()):
            # A stopped session (dead worker, -x, session timeout) leaves its queue unreported.
            lines.append(f"**{collected - sum(counts.values())} collected test(s) produced no report**: "
                         "the session stopped before running them.")
        lines.extend(_failure_table([row for row in data["reports"] if row["outcome"] == "failed"]))
        # An interrupted session still exports; only its journal names what it left running.
        lines.extend(_flight_table((_journal(root) or ([], []))[0]))
        if data.get("collection_failures"):
            lines.append(f"Collection failures: {len(data['collection_failures'])}.")
    else:
        lines.append("Case outcomes: **unknown**. No passing result is inferred.")
        journal = _journal(root)
        if journal is not None:
            running, failed = journal
            lines.extend(["", "The session ended before its final export. Its incremental journal "
                          f"recorded {len(running)} test(s) in flight and {len(failed)} failed report(s)."])
            lines.extend(_flight_table(running))
            lines.extend(_failure_table(failed))
    providers = []
    for path in sorted(root.glob("provider-*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(row, dict):
                raise ValueError("invalid provider projection")
            providers.append(row)
        except (OSError, ValueError):
            error = "a provider projection is unreadable"
    expected_providers = {row["canary_id"] for row in data.get("reports", [])
                          if row.get("canary_id")}
    recorded_providers = {row.get("canary_id") for row in providers}
    if expected_providers - recorded_providers:
        error = "provider result projections are missing"
    if any(row.get("diagnostics_errors") for row in providers):
        error = "provider evidence contains recorded capture gaps"
    for path in sorted(root.glob("browser/*/evidence.json")):
        try:
            browser = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(browser, dict):
                raise ValueError("invalid browser projection")
            if browser.get("diagnostics_incomplete"):
                error = "browser evidence contains recorded capture gaps"
        except (OSError, ValueError):
            error = "a browser projection is unreadable"
    if providers:
        lines.extend(["", "| Provider check | Outcome | Recorded cause |",
                      "| --- | --- | --- |"])
        for row in providers:
            lines.append(f"| {_cell(row.get('canary_id', row.get('nodeid', 'unknown')))} | "
                         f"{_cell(row.get('outcome', 'unknown'))} | "
                         f"{_cell(row.get('violation') or row.get('classification') or 'none recorded')} |")
    if artifact_url.startswith("https://"):
        lines.extend(["", f"[Download safe evidence]({artifact_url})"])
    incomplete = error or artifact_outcome != "success" or not artifact_url
    if incomplete:
        lines.extend(["", "**diagnostics_incomplete**: " +
                      _cell(error or f"artifact outcome={artifact_outcome}; artifact URL available={bool(artifact_url)}") +
                      ". The original producer outcome above is unchanged."])
    lines.extend(["", "Raw JUnit, exception bodies, credentials and private runtime stores are not in this export."])
    return "\n".join(lines) + "\n"


def _escaped(value, *, property_value: bool = False) -> str:
    """Workflow-command data: it cannot end the command line, nor a property its field."""
    text = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if property_value else text


def _annotation(title: str, message: str) -> str:
    return f"::error title={_escaped(title, property_value=True)}::{_escaped(message)}"


def render_annotations(roots, *, limit: int = 10) -> list[str]:
    """At most `limit` error commands over all directories, failed tests first."""
    failed, collection, running = [], [], []
    for root in roots:
        data, error = _final(root)
        if not error:
            failed += [row for row in data["reports"] if row["outcome"] == "failed"]
            rows = data.get("collection_failures")
            collection += [row["nodeid"] for row in (rows if isinstance(rows, list) else [])
                           if isinstance(row, dict) and isinstance(row.get("nodeid"), str)]
            running += (_journal(root) or ([], []))[0]
        elif (journal := _journal(root)) is not None:
            running += journal[0]
            failed += journal[1]
    lines = [_annotation("Failed test", f"{row['nodeid']} ("
                         + ", ".join(str(part) for part in (row["phase"], row.get("error_type")) if part) + ")")
             for row in failed]
    lines += [_annotation("Collection failed", node) for node in collection]
    lines += [_annotation("Test in flight when the session was killed", node) for node in running]
    shown = lines[:max(limit, 0)]
    if len(lines) > len(shown):
        shown.append(f"::notice::{len(lines) - len(shown)} more failed tests are listed in the job summary")
    return shown


def _named(nodes, *, shown: int = 20) -> str:
    nodes = sorted(nodes)
    return ", ".join(nodes[:shown]) + (f" … and {len(nodes) - shown} more" if len(nodes) > shown else "")


def _lane_shape(lane) -> bool:
    def strings(value):
        return isinstance(value, list) and all(isinstance(node, str) for node in value)
    if not isinstance(lane, dict) or not strings(lane.get("full")):
        return False
    shard = lane.get("shard")
    return "shard" not in lane or (
        isinstance(shard, list) and len(shard) == 2 and all(type(part) is int for part in shard)
        and strings(lane.get("assigned")))


def _lane_proofs(root: Path, *, sha: str, run_id: str, gaps: list, others: list | None = None) -> list[dict]:
    """UI lane projections beneath root that belong to this commit and run.

    Each results.json is judged where it lies, never by its directory name. One
    without a `ui_browser` block is another producer's (browser tools) or a session the
    guard refused before it recorded a lane: it proves no lane node and is collected in
    `others` for its session status only. An unreadable
    one, or a lane projection of another commit or run, is a gap.
    """
    proofs = []
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        if "results.json" not in files:
            continue
        name = (Path(directory) / "results.json").relative_to(root).as_posix()
        data, error = _final(Path(directory))
        if error:
            gaps.append(f"{name}: {error}")
            continue
        identity = data.get("github") if isinstance(data.get("github"), dict) else {}
        attempt = identity.get("run_attempt")
        if "ui_browser" not in data:
            if others is not None:
                others.append({"name": name, "exit": data.get("session_exit_code")})
            continue
        lane = data["ui_browser"]
        if not _lane_shape(lane):
            gaps.append(f"{name}: invalid ui_browser block")
        elif identity.get("sha") != sha or identity.get("run_id") != run_id:
            gaps.append(f"{name}: belongs to commit {identity.get('sha')} run "
                        f"{identity.get('run_id')}, expected commit {sha} run {run_id}")
        elif not (isinstance(attempt, str) and attempt.isdecimal()):
            gaps.append(f"{name}: run attempt is unknown")
        else:
            # The lane guard's own rule (tests/browser_lane.py LaneReconciliation): a node is
            # executed once it has a call phase or any non-passed report; a crash row is one.
            executed = {row["nodeid"] for row in data["reports"]
                        if row["phase"] in ("call", "crash") or row["outcome"] != "passed"}
            proofs.append({"name": name, "attempt": int(attempt), "lane": lane, "executed": executed,
                           "exit": data.get("session_exit_code")})
    return proofs


def _red_session(label: str, proof: dict) -> list[str]:
    """The gap of a proof whose session did not end with exit status 0."""
    status = proof["exit"]
    if type(status) is int and status == 0:
        return []
    return [f"{label}: its session ended with exit status {status if type(status) is int else 'unknown'}"]


def reconcile_shards(manifest_root: Path, shards_root: Path, *, count: int, sha: str,
                     run_id: str) -> tuple[list[str], list[str]]:
    """(gaps, notes) of one run's sharded UI lane; any gap makes the lane RED.

    AUTHORITATIVE, unlike every other reader in this module: the manifest is the
    unsharded collection's witness of the lane, each shard must prove it saw that
    same lane, took exactly its slice and executed all of it. A proof whose session
    ended with a non-zero exit status is a gap as well: what GitHub reports as the
    result of a matrix job after one leg is re-run is undocumented, so a red session
    is refused here and never left to the job results alone.
    """
    gaps, notes = [], []
    if not sha or not run_id or count < 1:
        return ["the expected commit, run id and a positive shard count are required"], notes
    full = None
    witnesses = _lane_proofs(manifest_root, sha=sha, run_id=run_id, gaps=gaps)
    gaps += [f"{proof['name']}: the manifest carries a shard block"
             for proof in witnesses if "shard" in proof["lane"]]
    witnesses = [proof for proof in witnesses if "shard" not in proof["lane"]]
    if not witnesses:
        gaps.append("manifest: no valid projection of the unsharded lane")
    else:
        manifest = max(witnesses, key=lambda proof: proof["attempt"])
        full = manifest["lane"]["full"]
        notes.append(f"manifest: {len(full)} nodes, from attempt {manifest['attempt']}")
        same = sum(proof["attempt"] == manifest["attempt"] for proof in witnesses)
        if same > 1:  # Two witnesses of one attempt cannot both be the manifest.
            gaps.append(f"manifest: {same} projections from attempt {manifest['attempt']}")
        gaps += _red_session("manifest", manifest)
        if not full or len(set(full)) != len(full):
            gaps.append("manifest: the lane is empty" if not full else "manifest: duplicate node ids: "
                        + _named(node for node, seen in Counter(full).items() if seen > 1))
            full = None
    by_shard, others = {}, []
    for proof in _lane_proofs(shards_root, sha=sha, run_id=run_id, gaps=gaps, others=others):
        index, total = proof["lane"].get("shard", (0, 0))
        if total != count or not 1 <= index <= count:
            gaps.append(f"{proof['name']}: " + (f"declares shard {index}/{total}, expected one of {count}"
                                                if total else "carries no shard block"))
        else:
            by_shard.setdefault(index, []).append(proof)
    proven, winners = set(), set()
    for index in range(1, count + 1):
        label = f"shard {index}/{count}"
        proofs = sorted(by_shard.get(index, []), key=lambda proof: proof["attempt"])
        if not proofs:
            gaps.append(f"{label}: no proof")
            continue
        proof = proofs[-1]
        winners.add(proof["name"].split("/", 1)[0])
        attempts = [candidate["attempt"] for candidate in proofs]
        if attempts.count(proof["attempt"]) > 1:
            gaps.append(f"{label}: {attempts.count(proof['attempt'])} proofs from attempt {proof['attempt']}")
        gaps += _red_session(label, proof)
        assigned, executed = proof["lane"]["assigned"], proof["executed"]
        notes.append(f"{label}: proof from attempt {proof['attempt']}"
                     + (f" (attempts present: {', '.join(map(str, attempts))})" if len(proofs) > 1 else "")
                     + f"; assigned {len(assigned)}, executed {len(executed & set(assigned))}")
        if full is not None and proof["lane"]["full"] != full:
            theirs, ours = set(proof["lane"]["full"]), set(full)
            gaps.append(f"{label}: its lane differs from the manifest"
                        + (f"; only in the shard: {_named(theirs - ours)}" if theirs - ours else "")
                        + (f"; only in the manifest: {_named(ours - theirs)}" if ours - theirs else "")
                        + ("; same nodes in another order" if theirs == ours else ""))
        # By position in the recorded list, exactly as the plugin slices it. The list is not
        # sorted here: an id the export redacts can sort elsewhere than the id that ran.
        if full is not None and assigned != full[index - 1::count]:
            gaps.append(f"{label}: its assignment is not slice {index} of {count} of the manifest lane")
        if set(assigned) - executed:
            gaps.append(f"{label}: assigned but not executed ({len(set(assigned) - executed)}): "
                        + _named(set(assigned) - executed))
        if executed - set(assigned):
            gaps.append(f"{label}: executed outside its assignment ({len(executed - set(assigned))}): "
                        + _named(executed - set(assigned)))
        proven |= executed & set(assigned)
    # A producer that shares a winning proof's artifact (browser tools) proves no lane node; its
    # red session is refused for the same reason a red shard is. One in the artifact of a
    # superseded attempt belongs to that attempt and is superseded with it.
    for other in others:
        if other["name"].split("/", 1)[0] in winners:
            gaps += _red_session(other["name"], other)
    if full is not None and set(full) - proven:
        gaps.append(f"lane: {len(set(full) - proven)} of {len(full)} manifest nodes are executed by no shard: "
                    + _named(set(full) - proven))
    return gaps, notes


def _reconcile(args) -> int:
    try:
        gaps, notes = reconcile_shards(args.manifest, args.shards, count=args.count,
                                       sha=args.sha, run_id=args.run_id)
    except Exception as error:  # An unreadable proof tree is a failed proof, not a skipped one.
        gaps, notes = [f"reconciliation failed ({type(error).__name__})"], []
    verdict = (f"INCOMPLETE — {len(gaps)} gap(s)" if gaps
               else f"complete — every node of the lane is executed by one of {args.count} shards")
    lines = [f"UI_BROWSER_RECONCILE {verdict}"] + notes + [f"GAP {gap}" for gap in gaps]
    # GitHub shows at most ten error annotations per step; the log and summary list every gap.
    lines += [_annotation("UI browser lane incomplete", gap) for gap in gaps[:10]]
    sys.stdout.buffer.write("".join(line + "\n" for line in lines).encode("utf-8", "replace"))
    sys.stdout.flush()
    try:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(
                [f"## UI browser lane reconciliation — {_cell(verdict)}", "",
                 f"Commit `{_cell(args.sha)}`, run {_cell(args.run_id)}. "
                 "A session that ended red is a gap; its shard job names the failed tests.",
                 ""] + [f"- {_cell(note)}" for note in notes]
                + [f"- **GAP** {_cell(gap)}" for gap in gaps]) + "\n")
    except OSError as error:  # The verdict stands without its rendering.
        print(f"::warning::UI lane reconciliation summary is unavailable ({type(error).__name__}).")
    return 1 if gaps else 0


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    summarize = commands.add_parser("summarize")
    summarize.add_argument("--evidence-dir", type=Path, required=True)
    summarize.add_argument("--summary", type=Path, required=True)
    summarize.add_argument("--producer-outcome", required=True)
    summarize.add_argument("--artifact-outcome", required=True)
    summarize.add_argument("--artifact-url", default="")
    summarize.add_argument("--scope", default="full")
    summarize.add_argument("--label", default="")
    annotate = commands.add_parser("annotate")
    annotate.add_argument("--evidence-dir", type=Path, action="append", required=True)
    annotate.add_argument("--limit", type=int, default=10)
    reconcile = commands.add_parser("reconcile-shards")
    for flag, kind in (("--manifest", Path), ("--shards", Path), ("--count", int),
                       ("--sha", str), ("--run-id", str), ("--summary", Path)):
        reconcile.add_argument(flag, type=kind, required=True)
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        if not stop.code or argv[:1] != ["annotate"]:
            raise
        # Annotations are advisory: neither usage nor unreadable evidence fails a step.
        print("::warning::CI annotations are unavailable (usage error).")
        return 0
    if args.command == "reconcile-shards":
        return _reconcile(args)
    if args.command == "annotate":
        try:
            lines = render_annotations(args.evidence_dir, limit=args.limit)
            # Runners decode step output as UTF-8 whatever the console code page is.
            sys.stdout.buffer.write("".join(line + "\n" for line in lines).encode("utf-8", "replace"))
        except Exception as error:
            print(f"::warning::CI annotations are unavailable ({type(error).__name__}).")
        return 0
    text = render_summary(args.evidence_dir, producer_outcome=args.producer_outcome,
                          artifact_outcome=args.artifact_outcome, artifact_url=args.artifact_url,
                          scope=args.scope, label=args.label)
    with args.summary.open("a", encoding="utf-8") as handle:
        handle.write(text)
    if "diagnostics_incomplete" in text:
        print("::warning::CI diagnostics are incomplete; original test outcome is unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

# pytest is intentionally below the stdlib-only CLI entrypoint and loaded only
# through conftest's post-isolation plugin registration.
import pytest


def pytest_addoption(parser):
    parser.addoption("--ci-evidence-dir", default=None,
                     help="Dedicated public projection directory; raw JUnit must be elsewhere.")


def pytest_configure(config):
    if output_dir(config) is not None:
        config.pluginmanager.register(_Results(config), "ci_safe_results")


def _redact(value, secrets):
    from ouroboros.observability import redact_projection
    from ouroboros.secret_masking import redact_known_values

    return redact_projection(redact_known_values(value, secrets)).value


class _Results:
    def __init__(self, config):
        from ouroboros.secret_masking import MASKED_SECRET_SETTING_KEYS

        self.config = config
        self.reports = []
        self.collection_failures = []
        self.secrets = tuple(os.environ[key] for key in MASKED_SECRET_SETTING_KEYS if os.environ.get(key))
        self.safe_text = {}
        self.journal = None
        if not hasattr(config, "workerinput"):  # xdist re-emits worker events on the controller.
            try:
                path = output_dir(config) / "events.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                # Unbuffered: each row reaches the OS before a kill can lose it.
                self.journal = path.open("wb", buffering=0)
            except Exception as error:
                self._journal_off(error)

    def _journal_off(self, error=None):
        journal, self.journal = self.journal, None
        try:
            if journal is not None:
                journal.close()
        except Exception as close_error:
            error = error or close_error
        if error is not None:  # One line, then silence: the session outcome is untouched.
            print(f"CI_DIAGNOSTICS_INCOMPLETE: incremental journal disabled ({type(error).__name__})")

    def _event(self, event, nodeid, **facts):
        if self.journal is None:
            return
        try:
            # A parametrized nodeid can carry a secret: every value is redacted
            # before its first write; the cache keeps that to once per distinct text.
            row = {"event": event, "nodeid": nodeid, **facts}
            for key, value in row.items():
                if value not in self.safe_text:
                    self.safe_text[value] = _redact(value, self.secrets)
                row[key] = self.safe_text[value]
            self.journal.write((json.dumps(row) + "\n").encode("ascii"))
        except Exception as error:
            self._journal_off(error)

    def pytest_unconfigure(self):
        self._journal_off()  # A session that never reached sessionfinish still releases the file.

    def pytest_runtest_logstart(self, nodeid):
        self._event("start", nodeid)

    def pytest_runtest_logfinish(self, nodeid):
        self._event("finish", nodeid)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        # The exception TYPE is useful; repr/str/longrepr can echo credentials.
        report.ci_error_type = type(call.excinfo.value).__name__ if call.excinfo else ""
        canary = getattr(getattr(item, "callspec", None), "params", {}).get("canary")
        report.ci_canary_id = getattr(canary, "canary_id", "")

    def pytest_runtest_logreport(self, report):
        duration = float(getattr(report, "duration", 0.0))
        # xdist reports a dead worker's test with when="???" and no worker-side attributes.
        crashed = report.when not in _PHASES
        row = {
            "nodeid": report.nodeid, "phase": "crash" if crashed else report.when,
            "outcome": report.outcome,
            "duration_seconds": duration if math.isfinite(duration) else None,
            "error_type": "WorkerCrash" if crashed else getattr(report, "ci_error_type", ""),
            "canary_id": getattr(report, "ci_canary_id", ""),
        }
        self.reports.append(row)
        self._event("report", row["nodeid"], phase=row["phase"], outcome=row["outcome"],
                    error_type=row["error_type"])

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_failures.append({"nodeid": report.nodeid, "outcome": "failed"})

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        yield  # Read the final session status after ordinary guards have run.
        if hasattr(self.config, "workerinput"):
            return  # xdist's controller receives the worker reports.
        try:
            facts = {
                "format": 1, "session_exit_code": int(session.exitstatus),
                "tests_collected": session.testscollected, "reports": self.reports,
                "collection_failures": self.collection_failures,
                "environment": {"python": platform.python_version(), "platform": platform.system(),
                                "pytest": pytest.__version__},
                "raw_exception_text": "omitted",
                "raw_junit": "private_not_copied",
                "github": {name: os.environ.get("GITHUB_" + name.upper(), "")
                           for name in ("sha", "run_id", "run_attempt")},
            }
            # tests/browser_lane.py: the complete lane, and this session's shard and slice.
            lane = getattr(self.config, "_ui_browser_lane", None)
            if lane:
                facts["ui_browser"] = lane
            write_json(output_dir(self.config) / "results.json", _redact(facts, self.secrets))
        except Exception as error:
            # Diagnostics alone never alter session.exitstatus or expose a body.
            print(f"CI_DIAGNOSTICS_INCOMPLETE: safe result export failed ({type(error).__name__})")
        finally:
            self._journal_off()
