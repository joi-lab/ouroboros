"""Public CI exports preserve producer exits, secret boundaries and full UI proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
CANARY, CANARY_PUSH = "provider-canary.yml", "provider-canary-push.yml"
PROVIDER_SECRETS = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY", "CLOUDRU_FOUNDATION_MODELS_BASE_URL", "GIGACHAT_CREDENTIALS",
)
SHARED_BRANCHES = ["main", "ouroboros", "ouroboros-stable"]
# What starts a PAID canary run on a branch push. The code workflow has no path list, so this
# one is spelled out here: a prompts- or skills-only push runs the code workflow and no canary.
CANARY_PUSH_PATHS = [
    "ouroboros/**", "supervisor/**", "server.py", "tests/**", "web/**", "site/**", "docs/**", "assets/**",
    "requirements-runtime.lock", "uv.lock", "pyproject.toml", ".github/workflows/**", ".github/actions/**",
    "build.sh", "build_linux.sh", "build_windows.ps1", "Dockerfile", "scripts/**", "devtools/**",
    "packaging/**", "android/**", "VERSION", "README.md", "CONTRIBUTING.md", "LICENSE",
    ".github/PULL_REQUEST_TEMPLATE.md", "launcher.py",
]
DOWNLOAD_ACTION = "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
UI_JOBS = ["scope", "ui-shard", "ui-manifest", "ui-smoke"]
# The complete lane takes no selection: its jobs run whenever browsers are needed.
FULL_LANE = "${{ needs.scope.outputs.run_browser == 'true' }}"
# The single-scenario PARTIAL DIAGNOSTIC is a job of the push wrapper, its only selector.
PARTIAL = ("ui-browser-push.yml", "ui-diagnostic")


def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def _steps(name, job):
    return {step["id"]: step for step in _workflow(name)["jobs"][job]["steps"] if "id" in step}


def test_provider_diagnostics_stay_outside_test_and_release_authority():
    workflow = _workflow("ci.yml")
    job = _workflow(CANARY)["jobs"]["integration-test"]
    steps = _steps(CANARY, "integration-test")
    producer = steps["provider_tests"]
    assert "continue-on-error" not in job and "continue-on-error" not in producer
    assert "continue-on-error" not in workflow["jobs"]["integration-test"]
    assert "pytest tests/test_provider_integration.py -m integration -q -rs --tb=short" in producer["run"]
    assert "--ci-evidence-dir=" in producer["run"] and "--junitxml=" in producer["run"]
    assert "ci-private/provider/results.xml" in producer["run"]
    assert "ci-evidence/provider" in producer["run"]
    assert "secrets." in str(producer["env"])
    for step in job["steps"]:
        if step.get("id") != "provider_tests":
            assert "secrets." not in str(step)
    assert "env" not in job
    upload, summary = steps["provider_evidence"], steps["provider_summary"]
    assert upload["uses"] == UPLOAD_ACTION
    assert upload["with"]["path"] == "${{ runner.temp }}/ci-evidence/provider/"
    assert upload["with"]["retention-days"] == 30
    assert "github.sha" in upload["with"]["name"] and "github.run_attempt" in upload["with"]["name"]
    assert summary["env"]["PRODUCER_OUTCOME"] == "${{ steps.provider_tests.outcome }}"
    assert summary["env"]["ARTIFACT_URL"] == "${{ steps.provider_evidence.outputs.artifact-url }}"
    assert "python -m tests.ci_evidence summarize" in summary["run"]
    assert list(steps).index("provider_evidence") < list(steps).index("provider_summary")
    assert workflow["jobs"]["release-preflight"]["needs"] == ["full-test", "integration-test", "system-e2e-mock"]
    release = workflow["jobs"]["release"]
    assert "needs.release-preflight.result == 'success'" in release["if"]


def test_only_informational_steps_tolerate_errors_and_report_missing_uploads():
    for name, jobname in ((CANARY, "integration-test"), ("ui-browser.yml", "ui-shard"), PARTIAL):
        job = _workflow(name)["jobs"][jobname]
        tolerant = [step for step in job["steps"] if step.get("continue-on-error")]
        assert tolerant
        for step in tolerant:
            assert "!cancelled()" in step["if"]
            assert (step.get("uses") == UPLOAD_ACTION or
                    "tests.ci_evidence summarize" in step.get("run", "") or
                    "tests/ci_evidence.py annotate" in step.get("run", "") or
                    "diagnostics_incomplete" in step.get("run", ""))
            assert "-m pytest" not in step.get("run", "")
        warning = next(step for step in job["steps"] if "Disclose incomplete" in step.get("name", ""))
        assert "outputs.artifact-url == ''" in warning["if"]
        assert "summary.outcome != 'success'" in warning["if"]
        assert "::warning::" in warning["run"] and "$GITHUB_STEP_SUMMARY" in warning["run"]
    # The coverage decision, the lane manifest and the lane verdict tolerate nothing:
    # there a failed step is the result.
    for jobname in ("scope", "ui-manifest", "ui-smoke"):
        job = _workflow("ui-browser.yml")["jobs"][jobname]
        assert "continue-on-error" not in job
        assert not any("continue-on-error" in step for step in job["steps"]), jobname


def _triggers(workflow):
    return workflow.get("on", workflow.get(True))  # PyYAML's YAML 1.1 spelling of "on".


def _canary_wiring_faults(ci, shared, push):
    """Name every way the canary body and its two callers can drift apart."""
    faults = []
    passed = {name: f"${{{{ secrets.{name} }}}}" for name in PROVIDER_SECRETS}
    for label, jobs in (("ci.yml", ci["jobs"]), (CANARY_PUSH, push["jobs"])):
        caller = jobs["integration-test"]
        if caller.get("uses") != f"./.github/workflows/{CANARY}":
            faults.append(f"{label} does not call the shared job")
        if caller.get("secrets") != passed:
            faults.append(f"{label} does not pass exactly the eight provider secrets by name")
        if caller.get("permissions") != {"contents": "read"}:
            faults.append(f"{label} widens the called job's permissions")
    # Exact key sets: a condition or a group on any of the three jobs changes how often the paid job runs.
    for label, job, keys in (("ci.yml", ci["jobs"]["integration-test"], {"if", "uses", "secrets", "permissions"}),
                             (CANARY_PUSH, push["jobs"]["integration-test"], {"uses", "secrets", "permissions"}),
                             (CANARY, shared["jobs"]["integration-test"], {"runs-on", "steps"})):
        if set(job) != keys:
            faults.append(f"{label} canary job carries keys other than {sorted(keys)}")
    declared = {name: {"required": False} for name in PROVIDER_SECRETS}
    if _triggers(shared) != {"workflow_call": {"secrets": declared}}:
        faults.append("the shared job is not call-only with eight optional secrets")
    # One paid run per push that touches the canary path list, on the three shared branches only.
    if _triggers(push) != {"push": {"branches": SHARED_BRANCHES, "paths": CANARY_PUSH_PATHS}}:
        faults.append("the push wrapper's trigger is not the three shared branches with the canary path list")
    # The code workflow sees EVERY push to those branches: a path list here leaves a landed commit untested.
    if _triggers(ci)["push"] != {"branches": SHARED_BRANCHES, "tags": ["v*"]}:
        faults.append("ci.yml's push trigger is not the three shared branches plus v* tags with no path filter")
    for label, workflow in ((CANARY, shared), (CANARY_PUSH, push)):
        if list(workflow["jobs"]) != ["integration-test"]:
            faults.append(f"{label} holds more than the canary job")
        if workflow.get("permissions") != {"contents": "read"}:
            faults.append(f"{label} does not default to read-only permissions")
        # A group would cancel or replace a paid run that belongs to another commit.
        if any("concurrency" in scope for scope in (workflow, *workflow["jobs"].values())):
            faults.append(f"{label} declares a concurrency group")
    return faults


def test_provider_canaries_share_one_body_between_the_code_workflow_and_the_push_wrapper():
    assert _canary_wiring_faults(*map(_workflow, ("ci.yml", CANARY, CANARY_PUSH))) == []
    callers = []
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for name, job in _workflow(path.name)["jobs"].items():
            assert job.get("secrets") != "inherit", path.name  # Would hand over signing and live-stand keys.
            if str(job.get("uses", "")).endswith(f"/{CANARY}"):
                callers.append((path.name, name))
    # A third caller is a second paid run for some event.
    assert callers == [("ci.yml", "integration-test"), (CANARY_PUSH, "integration-test")]


def test_provider_canary_workflows_are_protected_exactly_like_ci_yml():
    """`release-preflight` requires the provider-canary job, whose body and
    branch-push trigger live in two workflow files beside ci.yml; an inventory
    naming only the parent would leave the release canary editable."""
    from ouroboros.runtime_mode_policy import RELEASE_INVARIANT_PATHS, protected_path_category
    from scripts.run_external_review import _RELEASE_MACHINERY_PATHS

    parent = protected_path_category(".github/workflows/ci.yml")
    for name in (CANARY, CANARY_PUSH):
        path = f".github/workflows/{name}"
        assert (ROOT / path).is_file(), path
        assert path in RELEASE_INVARIANT_PATHS, path
        assert path in _RELEASE_MACHINERY_PATHS, path  # The contributor label follows the body too.
        assert protected_path_category(path) == protected_path_category(f"./{path}") == parent, path
    # The category follows the listed files, not the directory.
    assert protected_path_category(".github/workflows/unlisted.yml") == ""


CANARY_DRIFTS = {
    "wrapper also fires on tags": lambda w: _triggers(w.push)["push"].update(tags=["v*"]),
    "wrapper gains a second trigger": lambda w: _triggers(w.push).update(workflow_dispatch=None),
    "wrapper drops a branch": lambda w: _triggers(w.push)["push"]["branches"].remove("main"),
    "wrapper drops a path": lambda w: _triggers(w.push)["push"]["paths"].pop(),
    "wrapper gains a path": lambda w: _triggers(w.push)["push"]["paths"].append("prompts/**"),
    "ci.yml regains a path filter": lambda w: _triggers(w.ci)["push"].update(paths=["ouroboros/**"]),
    "ci.yml ignores a path": lambda w: _triggers(w.ci)["push"].update({"paths-ignore": ["prompts/**"]}),
    "ci.yml drops a branch": lambda w: _triggers(w.ci)["push"]["branches"].remove("main"),
    "wrapper concurrency": lambda w: w.push.update(concurrency="canary"),
    "shared job concurrency": lambda w: w.shared["jobs"]["integration-test"].update(concurrency="canary"),
    "inherited secrets": lambda w: w.ci["jobs"]["integration-test"].update(secrets="inherit"),
    "caller drops a secret": lambda w: w.push["jobs"]["integration-test"]["secrets"].pop("OPENAI_API_KEY"),
    "required secret": lambda w: _triggers(w.shared)["workflow_call"]["secrets"]["OPENAI_API_KEY"].update(
        required=True),
    "undeclared ninth secret": lambda w: _triggers(w.shared)["workflow_call"]["secrets"].update(EXTRA={}),
    "shared job self-triggers": lambda w: _triggers(w.shared).update(push=None),
    "caller leaves the shared job": lambda w: w.ci["jobs"]["integration-test"].update(uses="./other.yml"),
    "shared job loses its read-only default": lambda w: w.shared.pop("permissions"),
    "caller widens permissions": lambda w: w.push["jobs"]["integration-test"].update(permissions="write-all"),
    "wrapper job condition": lambda w: w.push["jobs"]["integration-test"].update(
        {"if": "github.ref == 'refs/heads/ouroboros-stable'"}),
    "shared job condition": lambda w: w.shared["jobs"]["integration-test"].update(
        {"if": "github.event_name == 'workflow_dispatch'"}),
    "code-workflow caller concurrency": lambda w: w.ci["jobs"]["integration-test"].update(concurrency="canary"),
}


@pytest.mark.parametrize("drift", CANARY_DRIFTS.values(), ids=list(CANARY_DRIFTS))
def test_each_provider_canary_wiring_drift_is_named(drift):
    files = SimpleNamespace(ci=_workflow("ci.yml"), shared=_workflow(CANARY), push=_workflow(CANARY_PUSH))
    drift(files)
    assert _canary_wiring_faults(files.ci, files.shared, files.push)
def _selected(job, event, selection):
    """Whether a push-wrapper job runs for an event: its `if`, two comparisons joined once."""
    facts = {"github.event_name": event, "inputs.diagnostic": selection}
    comparison = r"(github\.event_name|inputs\.diagnostic) (==|!=) '([a-z_]+)'"
    match = re.fullmatch(r"\$\{\{ " + comparison + r" (&&|\|\|) " + comparison + r" \}\}", job["if"])
    assert match, job["if"]
    left, right = (((facts[name] == value) is (operator == "==")) for name, operator, value
                   in (match.group(1, 2, 3), match.group(5, 6, 7)))
    return (left and right) if match[4] == "&&" else (left or right)


def test_manual_ui_selection_is_fixed_partial_and_never_a_paid_or_full_check():
    caller = _workflow("ui-browser-push.yml")
    triggers = caller.get("on", caller.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["ouroboros"]}
    selection = triggers["workflow_dispatch"]["inputs"]["diagnostic"]
    assert selection["type"] == "choice" and selection["default"] == "full"
    assert selection["options"] == ["full", "viewport", "inflight"]
    assert "PARTIAL DIAGNOSTIC" in caller["run-name"]
    assert list(caller["jobs"]) == ["ui-smoke", "ui-diagnostic"] and "secrets" not in str(caller)
    lane, partial_job = caller["jobs"]["ui-smoke"], caller["jobs"]["ui-diagnostic"]
    # Exactly one job runs: the complete lane for a push (no selection exists there) and
    # a manual `full`, the scenario for every other manual selection, a foreign one included.
    for event, choice, runs_lane in (("push", "", True), ("workflow_dispatch", "full", True),
                                     ("workflow_dispatch", "viewport", False),
                                     ("workflow_dispatch", "inflight", False),
                                     ("workflow_dispatch", "", False), ("workflow_dispatch", "tests", False)):
        assert _selected(lane, event, choice) is runs_lane, (event, choice)
        assert _selected(partial_job, event, choice) is not runs_lane, (event, choice)
    # The lane job is the shared lane and carries no selection into it.
    assert set(lane) == {"name", "if", "uses", "permissions"} and lane["name"] == "ui-smoke"
    assert lane["uses"] == "./.github/workflows/ui-browser.yml"
    # A partial run is named as one wherever it shows; the lane's names never say so.
    assert "format('PARTIAL DIAGNOSTIC UI - {0}', inputs.diagnostic)" in partial_job["name"]
    assert "format('PARTIAL DIAGNOSTIC UI - {0}', inputs.diagnostic)" in caller["run-name"]
    assert "uses" not in partial_job and "needs" not in partial_job and "strategy" not in partial_job

    shared_text = (ROOT / ".github/workflows/ui-browser.yml").read_text(encoding="utf-8")
    shared = _workflow("ui-browser.yml")
    # The shared lane is the complete lane only: no input, so no caller can select a part
    # of it, and no job of it is skipped whenever browsers run.
    assert shared.get("on", shared.get(True)) == {"workflow_call": None}
    assert "inputs." not in shared_text and "PARTIAL DIAGNOSTIC UI" not in shared_text
    assert list(shared["jobs"]) == UI_JOBS
    assert [shared["jobs"][name].get("if") for name in UI_JOBS] == [None, FULL_LANE, FULL_LANE, "${{ !cancelled() }}"]
    assert [shared["jobs"][name]["name"] for name in UI_JOBS] == [
        "ui-scope", "ui-shard (${{ matrix.shard }}/4)", "ui-manifest", "ui-smoke"]
    assert "with" not in _workflow("ci.yml")["jobs"]["ui-smoke"]
    shard, diagnostic = _steps("ui-browser.yml", "ui-shard"), _steps(*PARTIAL)
    full, partial = shard["ui_tests"], diagnostic["ui_diagnostic"]
    assert "--require-ui-browser" in full["run"] and "pytest tests/ -m ui_browser" in full["run"]
    # A partial scenario is neither guarded, sharded, reconciled nor followed by browser tools.
    for full_lane_only in ("--require-ui-browser", "--ui-browser-shard", "reconcile-shards",
                           "test_browser_tools_smoke", "matrix", "download-artifact"):
        assert full_lane_only not in str(partial_job), full_lane_only
        assert full_lane_only in shared_text, full_lane_only
    assert "-vv --tb=short" in _steps("ui-browser.yml", "ui-shard")["ui_tests"]["run"]
    assert '${{ inputs.diagnostic }}' not in partial["run"]
    assert partial["env"]["DIAGNOSTIC"] == "${{ inputs.diagnostic }}"
    assert "--scope \"$DIAGNOSTIC\"" in diagnostic["ui_summary"]["run"]
    assert diagnostic["ui_summary"]["env"]["DIAGNOSTIC"] == "${{ inputs.diagnostic }}"
    assert "--scope full " in shard["ui_summary"]["run"] and "DIAGNOSTIC" not in shard["ui_summary"]["env"]


@pytest.mark.parametrize("exported", [True, False], ids=["proof-present", "export-failed"])
@pytest.mark.parametrize("jobname, producer, proof_id, upload_id, directory", [
    ("ui-shard", "ui_tests", "ui_proof", "ui_evidence", "ui"),
    ("ui-manifest", "ui_manifest", "manifest_proof", "manifest_evidence", "ui-manifest"),
])
def test_an_attempt_without_a_published_proof_is_red_itself(tmp_path, jobname, producer, proof_id,
                                                              upload_id, directory, exported):
    """An attempt whose export or upload failed must not leave `ui-smoke` an earlier attempt's proof to accept."""
    job = _workflow("ui-browser.yml")["jobs"][jobname]
    steps = _steps("ui-browser.yml", jobname)
    proof, upload = steps[proof_id], steps[upload_id]
    assert "continue-on-error" not in proof and "continue-on-error" not in upload
    assert upload["with"]["if-no-files-found"] == "error"
    if jobname == "ui-shard":  # The shard's later steps run after a red lane; the manifest job stops at its first.
        assert proof["if"] == "${{ !cancelled() && steps.ui_tests.outcome != 'skipped' }}"
        assert "!cancelled()" in upload["if"]
    else:
        assert "if" not in proof and "if" not in upload
    order = [step.get("id") for step in job["steps"]]
    assert order.index(producer) < order.index(proof_id) < order.index(upload_id)
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("workflow shell unavailable on this host")
    if exported:
        target = tmp_path / "ci-evidence" / directory / "host" / "results.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")
    command = proof["run"].replace("${{ runner.temp }}", str(tmp_path))
    done = subprocess.run([bash, "-e", "-o", "pipefail", "-c", command], capture_output=True, timeout=10)
    assert (done.returncode == 0) is exported


def test_ui_producers_have_distinct_reports_and_only_the_public_parent_is_uploaded():
    shard, diagnostic = _steps("ui-browser.yml", "ui-shard"), _steps(*PARTIAL)
    run = "${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}"
    for steps in (shard, diagnostic):
        upload = steps["ui_evidence"]
        assert upload["uses"] == UPLOAD_ACTION
        assert upload["with"]["path"] == "${{ runner.temp }}/ci-evidence/ui/"
    # One artifact per shard and attempt: a matrix leg or a rerun never collides with another.
    assert shard["ui_evidence"]["with"]["name"] == "ui-ci-full-shard-${{ matrix.shard }}-" + run
    # A scenario's artifact carries its selection, which is never `full`: no lane proof pattern matches it.
    assert diagnostic["ui_evidence"]["with"]["name"] == "ui-ci-${{ inputs.diagnostic }}-" + run
    for steps, key, directory in ((shard, "ui_tests", "host"), (diagnostic, "ui_diagnostic", "host"),
                                  (shard, "browser_tools", "tools")):
        run = steps[key]["run"]
        assert f"ci-private/ui/{directory}.xml" in run
        assert f"ci-evidence/ui/{directory}" in run
        assert "continue-on-error" not in steps[key]
    assert shard["ui_summary"]["env"]["PRODUCER_OUTCOME"] == "${{ steps.ui_tests.outcome }}"
    assert diagnostic["ui_summary"]["env"]["PRODUCER_OUTCOME"] == "${{ steps.ui_diagnostic.outcome }}"
    assert shard["tools_summary"]["env"]["PRODUCER_OUTCOME"] == "${{ steps.browser_tools.outcome }}"
    assert "ci-evidence/ui/host" in shard["ui_summary"]["run"]
    assert "ci-evidence/ui/host" in diagnostic["ui_summary"]["run"]
    assert "ci-evidence/ui/tools" in shard["tools_summary"]["run"]
    assert '--label "shard ${{ matrix.shard }}/4"' in shard["ui_summary"]["run"]
    annotate = shard["ui_annotations"]["run"]
    assert "python -I -S tests/ci_evidence.py annotate" in annotate
    assert "ci-evidence/ui/host" in annotate and "ci-evidence/ui/tools" in annotate


def _run_shell(step, tmp_path, *, diagnostic="viewport", result=0):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("workflow shell unavailable on this host")
    args = tmp_path / "args"
    command = step["run"].replace("${{ runner.temp }}", str(tmp_path)).replace("${{ matrix.shard }}", "2")
    stub = 'python() { printf "%s\\n" "$@" > "$ARGV_FILE"; return "$PRODUCER_EXIT"; }\n'
    env = {**os.environ, "ARGV_FILE": str(args), "PRODUCER_EXIT": str(result), "DIAGNOSTIC": diagnostic}
    completed = subprocess.run([bash, "-e", "-o", "pipefail", "-c", stub + command],
                               env=env, capture_output=True, text=True, encoding="utf-8", timeout=10)
    return completed, args.read_text(encoding="utf-8").splitlines() if args.exists() else []


@pytest.mark.parametrize("result", [0, 17])
@pytest.mark.parametrize("workflow,job,step", [
    (CANARY, "integration-test", "provider_tests"),
    ("ui-browser.yml", "ui-shard", "ui_tests"),
    (*PARTIAL, "ui_diagnostic"),
    ("ui-browser.yml", "ui-shard", "browser_tools"),
    ("ui-browser.yml", "ui-manifest", "ui_manifest"),
    *[("ci.yml", job, f"tests_{label}") for job in ("quick-test", "full-test")
      for label in ("parallel", "serial", "size")],
])
def test_actual_producer_shell_preserves_success_and_failure_exit(tmp_path, workflow, job, step, result):
    completed, args = _run_shell(_steps(workflow, job)[step], tmp_path, result=result)
    assert completed.returncode == result, completed.stderr
    assert args.count("pytest") == 1 and "summarize" not in args


OFFICIAL, FORK, TAG = "razzant/ouroboros", "someone/private-copy", "refs/tags/v7.5.2"


@pytest.mark.serial
@pytest.mark.parametrize("event,ref,repository,flag", [
    ("push", TAG, OFFICIAL, "--enforce"),
    ("workflow_dispatch", TAG, OFFICIAL, "--enforce"),  # A manual run on a tag ref can publish too.
    ("push", "refs/heads/ouroboros", OFFICIAL, ""),
    ("workflow_dispatch", "refs/heads/candidate", OFFICIAL, ""),
    ("schedule", "refs/heads/main", OFFICIAL, ""),
    # A fork without the ten provider keys keeps releasing its own tags.
    ("push", TAG, FORK, ""),
    ("workflow_dispatch", TAG, FORK, ""),
])
def test_release_floor_is_the_last_canary_step_and_enforces_only_on_an_official_release_tag(
        tmp_path, monkeypatch, event, ref, repository, flag):
    from tests.test_platform_ci_events import _value

    job = _workflow(CANARY)["jobs"]["integration-test"]
    floor = job["steps"][-1]
    assert floor["name"] == "Release floor for required provider canaries"
    # Its exit IS the floor: a tolerated or conditional step would let a tag release past it.
    assert set(floor) == {"name", "if", "env", "run"} and floor["if"] == "${{ !cancelled() }}"
    assert "secrets." not in str(floor) and list(floor["env"]) == ["FLOOR_ENFORCE"]
    # It reads the report the producer wrote, by the same path string.
    produced = re.search(r'--junitxml=("[^"]+")', _steps(CANARY, "integration-test")["provider_tests"]["run"])
    assert floor["run"] == ("python -m tests.provider_release_floor "
                            f'--junit {produced.group(1)} --summary "$GITHUB_STEP_SUMMARY" $FLOOR_ENFORCE')
    assert _value(floor["env"]["FLOOR_ENFORCE"], event=event, ref=ref, repository=repository) == flag
    # The real shell passes the flag as its own argument, or no argument at all, and keeps the reader's exit.
    monkeypatch.setenv("FLOOR_ENFORCE", flag)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    completed, args = _run_shell(floor, tmp_path, result=1)
    assert completed.returncode == 1, completed.stderr
    assert args[:3] == ["-m", "tests.provider_release_floor", "--junit"] and args[4] == "--summary"
    assert args[3].endswith("/ci-private/provider/results.xml") and args[5].endswith("summary.md")
    assert args[6:] == ([flag] if flag else [])


@pytest.mark.parametrize("selection,target", [
    ("viewport", "tests/test_ui_smoke_playwright.py::test_ui_smoke_live_card_mutations_preserve_viewport"),
    ("inflight", "tests/test_ui_smoke_inflight_indicator.py::test_ui_smoke_chat_inflight_indicator_lifecycle"),
])
def test_partial_selection_passes_exact_target_as_argv(tmp_path, selection, target):
    completed, args = _run_shell(_steps(*PARTIAL)["ui_diagnostic"], tmp_path, diagnostic=selection)
    assert completed.returncode == 0 and target in args
    assert "--require-ui-browser" not in args and "tests/test_browser_tools_smoke.py" not in args
    assert "-k" not in args and "tests/" not in args
    assert not any(arg.startswith("--ui-browser-shard") for arg in args)


def test_unknown_or_shell_like_selection_cannot_execute_or_silently_run_full(tmp_path):
    marker = tmp_path / "injected"
    step = _steps(*PARTIAL)["ui_diagnostic"]
    completed, args = _run_shell(step, tmp_path, diagnostic=f"viewport; touch {marker}")
    assert completed.returncode == 2 and not args and not marker.exists()
    # `full` and an absent selection are no scenario either: nothing here runs the lane.
    for selection in ("full", "", "FULL", "tests/"):
        completed, args = _run_shell(step, tmp_path, diagnostic=selection)
        assert completed.returncode == 2 and not args, selection


def test_every_added_outcome_reference_names_an_already_declared_step():
    pattern = re.compile(r"steps\.([A-Za-z_][A-Za-z0-9_]*)\.")
    for workflow, job in ((CANARY, "integration-test"), *(("ui-browser.yml", job) for job in UI_JOBS),
                          PARTIAL):
        known = set()
        for step in _workflow(workflow)["jobs"][job]["steps"]:
            references = pattern.findall(str(step))
            assert set(references) <= known, (workflow, step.get("name"), references, known)
            if step.get("id"):
                known.add(step["id"])


@pytest.mark.serial
def test_full_lane_runs_as_four_guarded_shards_under_step_ceilings(tmp_path):
    job = _workflow("ui-browser.yml")["jobs"]["ui-shard"]
    steps = _steps("ui-browser.yml", "ui-shard")
    assert job["needs"] == "scope" and job["if"] == FULL_LANE
    assert job["strategy"] == {"fail-fast": False, "matrix": {"shard": [1, 2, 3, 4]}}
    install, lane, tools = steps["install_browsers"], steps["ui_tests"], steps["browser_tools"]
    # Every producer step has its own ceiling, and their sum stays under the job's: a step
    # ceiling fails the step and leaves the evidence steps running, the job ceiling cancels them.
    ceilings = (install["timeout-minutes"], lane["timeout-minutes"], tools["timeout-minutes"])
    assert ceilings == (45, 90, 20) and sum(ceilings) < job["timeout-minutes"] == 170
    assert "chromium webkit" in install["run"]
    for producer in (lane, tools):
        assert producer["env"]["OUROBOROS_EXPECT_BROWSER_ENGINES"] == "chromium,webkit"
    completed, args = _run_shell(lane, tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert args[args.index("pytest") + 1:][:4] == ["tests/", "-m", "ui_browser", "--require-ui-browser"]
    assert "--ui-browser-shard=2/4" in args and "--session-timeout=4500" in args
    # The session budget is cooperative; nothing kills a test mid-flight or narrows the lane.
    assert not any(arg.startswith(("--timeout", "-k", "--deselect", "--ignore", "--lf", "-x", "-n"))
                   for arg in args)
    assert "--ui-browser-shard=${{ matrix.shard }}/4" in lane["run"]
    # Browser tools keep their manual/tag condition, independent of the host lane's outcome, on one shard.
    assert "matrix.shard == 1" in tools["if"] and "steps.ui_tests" not in tools["if"]
    assert "github.event_name == 'workflow_dispatch' || startsWith(github.ref, 'refs/tags/v')" in tools["if"]


@pytest.mark.serial
def test_lane_manifest_is_an_unsharded_collection_whose_upload_is_mandatory(tmp_path):
    jobs = _workflow("ui-browser.yml")["jobs"]
    job, steps = jobs["ui-manifest"], _steps("ui-browser.yml", "ui-manifest")
    assert job["needs"] == "scope" and job["if"] == jobs["ui-shard"]["if"] and "strategy" not in job
    for shard_only in ("--ui-browser-shard", "matrix", "playwright install", "--session-timeout"):
        assert shard_only not in str(job), shard_only
    collect, lane = steps["ui_manifest"], _steps("ui-browser.yml", "ui-shard")["ui_tests"]
    completed, args = _run_shell(collect, tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert args[args.index("pytest") + 1:][:5] == [
        "tests/", "-m", "ui_browser", "--require-ui-browser", "--collect-only"]
    assert "safe_test.py" in collect["run"] and "ci-evidence/ui-manifest/host" in collect["run"]
    # The witness collects under the environment the shards collect with.
    for name in ("OUROBOROS_RUN_UI_SMOKE", "OUROBOROS_EXPECT_BROWSER_ENGINES"):
        assert collect["env"][name] == lane["env"][name]
    upload = steps["manifest_evidence"]
    assert upload["uses"] == UPLOAD_ACTION and "if" not in upload
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["path"] == "${{ runner.temp }}/ci-evidence/ui-manifest/"
    assert upload["with"]["name"] == ("ui-ci-full-manifest-${{ github.sha }}-"
                                      "${{ github.run_id }}-${{ github.run_attempt }}")


def _pattern(name):
    """The download pattern that selects every shard and attempt of one upload name."""
    for expression, value in (("matrix.shard", "*"), ("github.run_attempt", "*")):
        name = name.replace(f"${{{{ {expression} }}}}", value)
    return name


def test_aggregator_reconciles_this_runs_shards_against_the_manifest_without_tolerance():
    jobs = _workflow("ui-browser.yml")["jobs"]
    job = jobs["ui-smoke"]
    # Every need is a job that runs whenever browsers do: the aggregator, and through it
    # the caller's `ui-smoke` result a release reads, never waits on an always-skipped job.
    assert job["needs"] == ["scope", "ui-shard", "ui-manifest"]
    assert job["if"] == "${{ !cancelled() }}" and job["timeout-minutes"] == 15
    verdict = _steps("ui-browser.yml", "ui-smoke")["verdict"]
    assert job["steps"][0] == verdict and "if" not in verdict
    assert "${{" not in verdict["run"], "every expression reaches the shell through env"
    assert verdict["env"] == {
        "SCOPE_RESULT": "${{ needs.scope.result }}",
        "RUN_BROWSER": "${{ needs.scope.outputs.run_browser }}",
        "SHARD_RESULT": "${{ needs.ui-shard.result }}",
        "MANIFEST_RESULT": "${{ needs.ui-manifest.result }}"}
    assert jobs["scope"]["outputs"] == {"run_browser": "${{ steps.scope.outputs.run_browser }}"}
    proof = job["steps"][1:]
    # Every proof step runs whenever browsers are needed, whatever the verdict step found:
    # a red shard's unexecuted nodes are named, and a red session is refused by its proof.
    for step in proof:
        assert step["if"] == FULL_LANE.replace("${{ ", "${{ !cancelled() && "), step
    checkout, manifest, shards, reconcile = proof
    assert checkout["uses"].startswith("actions/checkout@")
    assert manifest["uses"] == shards["uses"] == DOWNLOAD_ACTION
    assert manifest["with"]["pattern"] == _pattern(
        _steps("ui-browser.yml", "ui-manifest")["manifest_evidence"]["with"]["name"])
    assert shards["with"]["pattern"] == _pattern(
        _steps("ui-browser.yml", "ui-shard")["ui_evidence"]["with"]["name"])
    assert manifest["with"]["path"] != shards["with"]["path"]
    assert all("merge-multiple" not in step["with"] and "name" not in step["with"] for step in (manifest, shards))
    run = reconcile["run"]
    assert run.startswith("python3 -I -S tests/ci_evidence.py reconcile-shards ")
    assert f'--manifest "{manifest["with"]["path"]}"' in run and f'--shards "{shards["with"]["path"]}"' in run
    assert f'--count {len(jobs["ui-shard"]["strategy"]["matrix"]["shard"])} ' in run
    assert '--sha "${{ github.sha }}" --run-id "${{ github.run_id }}"' in run
    assert "continue-on-error" not in reconcile and "|| true" not in run


def _verdict(tmp_path, **results):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("workflow shell unavailable on this host")
    summary = tmp_path / "summary.md"
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary), "SCOPE_RESULT": "success",
           "RUN_BROWSER": "true", "SHARD_RESULT": "success", "MANIFEST_RESULT": "success", **results}
    completed = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
         _steps("ui-browser.yml", "ui-smoke")["verdict"]["run"]],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    text = summary.read_text(encoding="utf-8", errors="replace") if summary.exists() else ""
    return completed.returncode, completed.stdout, text


SKIPPED_LANE = {"SHARD_RESULT": "skipped", "MANIFEST_RESULT": "skipped"}


@pytest.mark.serial
@pytest.mark.parametrize("results, path, green, said", [
    ({}, "full", True, []),
    ({"SHARD_RESULT": "failure"}, "full", False, ["ui-shard: failure"]),
    ({"SHARD_RESULT": "cancelled"}, "full", False, ["ui-shard: cancelled"]),
    ({"SHARD_RESULT": "skipped"}, "full", False, ["ui-shard: skipped"]),
    ({"MANIFEST_RESULT": "failure"}, "full", False, ["ui-manifest: failure"]),
    ({"MANIFEST_RESULT": "failure", "SHARD_RESULT": "failure"}, "full", False,
     ["ui-manifest: failure", "ui-shard: failure"]),
    ({"MANIFEST_RESULT": "skipped"}, "full", False, ["ui-manifest: skipped"]),
    ({"MANIFEST_RESULT": "", "SHARD_RESULT": ""}, "full", False, ["ui-manifest: none", "ui-shard: none"]),
    ({"RUN_BROWSER": "false", **SKIPPED_LANE}, "documentation", True, []),
    # An unknown coverage decision is RED, never "not run".
    ({"SCOPE_RESULT": "failure", "RUN_BROWSER": "", **SKIPPED_LANE}, "unknown", False,
     ["coverage decision is unknown (scope job: failure, run_browser: none)"]),
    ({"SCOPE_RESULT": "cancelled", "RUN_BROWSER": "false", **SKIPPED_LANE}, "unknown", False,
     ["coverage decision is unknown (scope job: cancelled, run_browser: false)"]),
    ({"SCOPE_RESULT": "failure", "RUN_BROWSER": "false", **SKIPPED_LANE}, "unknown", False,
     ["coverage decision is unknown (scope job: failure, run_browser: false)"]),
    ({"RUN_BROWSER": "", **SKIPPED_LANE}, "unknown", False, ["run_browser: none"]),
    ({"RUN_BROWSER": "maybe", **SKIPPED_LANE}, "unknown", False, ["run_browser: maybe"]),
    # A selection reaches this shell from nowhere: one in its environment changes nothing.
    ({"DIAGNOSTIC": "viewport", "DIAGNOSTIC_RESULT": "success", **SKIPPED_LANE}, "full", False,
     ["ui-manifest: skipped", "ui-shard: skipped"]),
], ids=["full-green", "shard-failed", "shard-cancelled", "shard-skipped", "manifest-failed", "both-failed",
        "manifest-skipped", "lane-silent", "documentation-only", "scope-failed", "scope-cancelled",
        "scope-failed-after-deciding", "scope-silent", "scope-garbage", "no-diagnostic-path"])
def test_aggregator_verdict_shell_has_two_explicit_paths(tmp_path, results, path, green, said):
    code, stdout, summary = _verdict(tmp_path, **results)
    assert code == int(not green), stdout
    for text in said:
        assert text in summary, summary
    assert ("UI browser lane RED" in summary) is not green
    assert ("::error title=UI browser lane::" in stdout) is not green
    assert ("event changes documentation only." in summary) is (path == "documentation")
    assert "PARTIAL DIAGNOSTIC" not in summary
    if path == "full" and green:
        assert summary == "", "a green full lane is announced by the reconciliation, not by this step"


@pytest.mark.serial
def test_real_reconciler_fails_the_aggregator_step_when_no_shard_reported(tmp_path):
    """The workflow's own reconcile command, run as written: an empty download is RED."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("workflow shell unavailable on this host")
    summary = tmp_path / "summary.md"
    run = _steps("ui-browser.yml", "ui-smoke")["reconcile"]["run"]
    for expression, value in (("runner.temp", str(tmp_path)), ("github.sha", "c" * 40), ("github.run_id", "31")):
        run = run.replace(f"${{{{ {expression} }}}}", value)
    assert "${{" not in run
    completed = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
         'python3() { "$REAL_PYTHON" "$@"; }\n' + run],
        cwd=ROOT, env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary), "REAL_PYTHON": sys.executable},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "manifest: no valid projection of the unsharded lane" in completed.stdout
    assert all(f"shard {index}/4: no proof" in completed.stdout for index in (1, 2, 3, 4))
    assert "INCOMPLETE" in summary.read_text(encoding="utf-8")


EVIDENCE_ACTION = "./.github/actions/test-evidence"
PASSES = ("parallel", "serial", "size")


def _evidence_action():
    return yaml.safe_load((ROOT / EVIDENCE_ACTION / "action.yml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("jobname,artifact", [
    ("quick-test", "quick-test"), ("full-test", "full-test-${{ matrix.os }}"),
])
def test_ordinary_jobs_name_their_failures_without_owning_the_result(jobname, artifact):
    job = _workflow("ci.yml")["jobs"][jobname]
    steps = _steps("ci.yml", jobname)
    for label in PASSES:
        producer = steps[f"tests_{label}"]
        assert "continue-on-error" not in producer
        assert f'--ci-evidence-dir="${{{{ runner.temp }}}}/ci-evidence/{label}"' in producer["run"]
        # A hang fails the step, so the evidence step still runs; a job limit would cancel it.
        assert isinstance(producer["timeout-minutes"], int)
    assert "continue-on-error" not in job and "timeout-minutes" not in job
    evidence = next(step for step in job["steps"] if step.get("uses") == EVIDENCE_ACTION)
    assert evidence["continue-on-error"] is True and "!cancelled()" in evidence["if"]
    assert evidence["with"] == {"name": artifact, "passes": " ".join(
        f"{label}=${{{{ steps.tests_{label}.outcome }}}}" for label in PASSES)}
    assert job["steps"].index(evidence) > max(job["steps"].index(steps[f"tests_{label}"])
                                              for label in PASSES)


def test_evidence_action_steps_are_independent_diagnostics():
    upload, report = _evidence_action()["runs"]["steps"]
    for step in (upload, report):  # Neither waits for the other's success nor fails the caller.
        assert step["if"] == "${{ !cancelled() }}" and step["continue-on-error"] is True
    assert upload["uses"] == UPLOAD_ACTION
    assert upload["with"]["path"] == "${{ runner.temp }}/ci-evidence/"
    for fact in ("inputs.name", "github.run_id", "github.run_attempt"):
        assert fact in upload["with"]["name"]
    assert report["shell"] == "bash"  # One shell on all three runner systems.
    assert report["env"]["ARTIFACT_OUTCOME"] == "${{ steps.upload.outcome }}"
    assert report["env"]["ARTIFACT_URL"] == "${{ steps.upload.outputs.artifact-url }}"
    assert "python -I -S tests/ci_evidence.py summarize" in report["run"]
    assert "python -I -S tests/ci_evidence.py annotate" in report["run"]
    assert "pytest" not in report["run"] and "${{" not in report["run"]


def _publish(tmp_path, passes):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("workflow shell unavailable on this host")
    summary = tmp_path / "summary.md"
    env = {**os.environ, "PASSES": passes, "ARTIFACT_OUTCOME": "success",
           "ARTIFACT_URL": "https://github.com/example/project/actions/runs/1/artifacts/2",
           "EVIDENCE_ROOT": str(tmp_path / "ci-evidence"), "GITHUB_STEP_SUMMARY": str(summary),
           "REAL_PYTHON": sys.executable}
    script = 'python() { "$REAL_PYTHON" "$@"; }\n' + _evidence_action()["runs"]["steps"][1]["run"]
    completed = subprocess.run([bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                               cwd=ROOT, env=env, capture_output=True, text=True,
                               encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    errors = [line for line in completed.stdout.splitlines() if line.startswith("::error")]
    return errors, summary.read_text(encoding="utf-8") if summary.exists() else ""


def _final_projection(directory, outcome, error_type=""):
    directory.mkdir(parents=True)
    (directory / "results.json").write_text(json.dumps({
        "format": 1, "session_exit_code": int(outcome == "failed"), "tests_collected": 1,
        "collection_failures": [], "github": {},
        "reports": [{"nodeid": "t.py::test_a", "phase": "call", "outcome": outcome,
                     "error_type": error_type}]}), encoding="utf-8")


def test_published_evidence_names_failed_killed_and_silent_passes(tmp_path):
    root = tmp_path / "ci-evidence"
    _final_projection(root / "parallel", "failed", "KeyError")
    (root / "serial").mkdir()
    (root / "serial" / "events.jsonl").write_text(
        json.dumps({"event": "start", "nodeid": "t.py::test_b"}) + "\n", encoding="utf-8")
    errors, summary = _publish(tmp_path, "parallel=failure serial=failure size=failure idle=skipped")
    assert errors == [
        "::error title=No test evidence::The size pass failed before any test reported; "
        "read that step's log.",
        "::error title=Failed test::t.py::test_a (call, KeyError)",
        "::error title=Test in flight when the session was killed::t.py::test_b",
    ]
    for label in PASSES:
        assert f"## CI test evidence — {label} pass" in summary
    assert "idle pass" not in summary  # A skipped producer ran nothing and reports nothing.
    assert "| t.py::test_a | call | KeyError |" in summary
    assert "| t.py::test_b |" in summary


def test_published_evidence_is_quiet_for_green_passes(tmp_path):
    _final_projection(tmp_path / "ci-evidence" / "parallel", "passed")
    errors, summary = _publish(tmp_path, "parallel=success serial=skipped size=skipped")
    assert errors == []
    assert "## CI test evidence — parallel pass" in summary and "passed=1, failed=0" in summary
    assert "serial pass" not in summary and "diagnostics_incomplete" not in summary
