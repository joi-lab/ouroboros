"""Delegated-run health obligations follow ownership.

The obligations stay globally visible (a preserved-and-invisible result is how
work rots on disk), but only the OWNER task receives each run's line with its
call-shaped instruction: ``integrate_delegated_patch`` refuses a non-owner with
``run_not_owned`` while the owner task is LIVE, and the read acknowledgement
only credits the owner. Another task's runs fold into ONE line per owner task
(owner decision Q3=A, 18.10.2026: 223 such lines, ~110k characters, filled one
task's Health section), with the run ids, the authority rule (once the owner is
terminal, reject from any root, apply from the recorded target) and a run's
exact reader.
"""

import types

import ouroboros.context_health as context_health
from ouroboros.delegate_custody import RunCustody
from tests._governance_docs_shared import architecture_text


def _env(tmp_path):
    env = types.SimpleNamespace()
    env.drive_root = tmp_path
    env.repo_path = lambda *parts: tmp_path / "repo-none" / "/".join(parts)
    env.drive_path = lambda *parts: tmp_path / "/".join(parts)
    return env


def _foreign_runs(monkeypatch):
    unread = RunCustody(run_id="run-u", task_id="task-owner")
    unread.output_artifact = "delegated/run-u.out"
    patch = RunCustody(run_id="run-p", task_id="task-owner")
    patch.patch_captured = True
    patch.target_root = "/tmp/target"
    monkeypatch.setattr(
        context_health, "build_health_invariants", context_health.build_health_invariants
    )
    import ouroboros.delegate_custody as custody

    monkeypatch.setattr(
        custody, "settled_unread_outputs", lambda root, state=None: [unread])
    monkeypatch.setattr(
        custody, "undisposed_patches", lambda root, state=None: [patch])


def test_non_owner_sees_one_line_per_owner_task_and_no_call_shaped_instruction(tmp_path, monkeypatch):
    _foreign_runs(monkeypatch)
    text = context_health.build_health_invariants(_env(tmp_path), task_id="task-other")
    rows = [line for line in text.splitlines() if "task-owner" in line]
    assert len(rows) == 1 and "ANOTHER TASK'S DELEGATED WORK — owner task task-owner" in rows[0]
    assert "results not read to EOF: run-u" in rows[0] and "patches awaiting apply/reject: run-p" in rows[0]
    assert "DELEGATED RESULT NEVER READ" not in text and "DELEGATED PATCH AWAITS DISPOSITION" not in text
    # The obligation is visible with its rule and reader; the DIRECTLY CALLABLE shape is not.
    assert "integrate_delegated_patch(run_id='run-p'" not in text
    assert "once it is terminal, a live top-level task may reject them from any root" in rows[0]
    assert "apply them from the recorded target" in rows[0] and "disposition row records who acted" in rows[0]
    assert "query_data(op='json', kind='task_result', ids=['task-owner'])" in rows[0]  # readable from any task
    assert "or a fresh payload binding" in rows[0]
    assert "read_file" not in text


def test_each_owner_task_gets_one_line_and_own_runs_keep_theirs(tmp_path, monkeypatch):
    import ouroboros.delegate_custody as custody
    from ouroboros.delegate_custody import RunCustody

    def unread(run_id, task):
        run = RunCustody(run_id=run_id, task_id=task)
        run.output_artifact = f"delegated/{run_id}.out"
        return run

    runs = [unread(f"run-a{i}", "task-a") for i in range(12)] + [unread("run-b0", "task-b"), unread("run-own", "me")]
    monkeypatch.setattr(custody, "settled_unread_outputs", lambda root, state=None: runs)
    monkeypatch.setattr(custody, "undisposed_patches", lambda root, state=None: [])
    text = context_health.build_health_invariants(_env(tmp_path), task_id="me")
    folded = [line for line in text.splitlines() if "ANOTHER TASK'S DELEGATED WORK" in line]
    assert [line.split("owner task ")[1].split(":")[0] for line in folded] == ["task-a", "task-b"]
    assert all(f"run-a{i}" in folded[0] for i in range(12))
    assert "DELEGATED RESULT NEVER READ — run run-own" in text and "run-own" not in "".join(folded)


def test_architecture_states_the_terminal_owner_apply_reject_authority_split():
    architecture = architecture_text()
    assert "PATCH_DISPOSED" in architecture
    assert "disposed_by_task_id" in architecture


def test_owner_keeps_the_call_shaped_instruction(tmp_path, monkeypatch):
    _foreign_runs(monkeypatch)
    text = context_health.build_health_invariants(_env(tmp_path), task_id="task-owner")
    assert "integrate_delegated_patch(run_id='run-p'" in text
    assert "read_file" in text


def test_unattributed_reader_keeps_the_call_shape(tmp_path, monkeypatch):
    """Background Consciousness and legacy callers pass no task id; they may
    be the owner, so the call-shaped wording survives."""
    _foreign_runs(monkeypatch)
    text = context_health.build_health_invariants(_env(tmp_path))
    assert "integrate_delegated_patch(run_id='run-p'" in text
    assert "read_file" in text


def test_one_custody_read_per_health_invariant_build(tmp_path, monkeypatch):
    """I18: the delegated-run lines share ONE custody read, and it is selected.

    Reading every custody row per block, and then per build, decoded the whole
    settled history each time; the shared read now selects the rows the lines
    can name (own, held, maybe-unread) and no full read runs unless it fails.
    SQL still visits every row, so the read stays O(rows) in the store.
    """
    import ouroboros.delegate_custody as custody
    import ouroboros.delegate_custody_current as current

    calls: list = []
    replays: list = []
    real_select, real_replay = current.health_runs, custody.replay
    monkeypatch.setattr(
        current, "health_runs",
        lambda root, task_id="": calls.append(root) or real_select(root, task_id),
    )
    monkeypatch.setattr(
        custody, "replay",
        lambda root, rows=None: replays.append(root) or real_replay(root, rows),
    )

    context_health.build_health_invariants(_env(tmp_path), task_id="task-owner")

    assert len(calls) == 1
    assert replays == []


# --- P6-3: the same obligation, for a reader whose own root already reaches it ---
#
# The recovery root's prompt carried sixteen near-identical abstract rows: the
# foreign branch keys on task identity alone, so it printed the rule even where
# the run's recorded target WAS that reader's own active root. `active_root` is
# threaded from the call site (the module has no ctx) and answered with the same
# predicate the apply gate asks, so the two can never disagree.


def _foreign_patch_targeting(monkeypatch, target_root):
    """One FOREIGN undisposed patch recorded against ``target_root``."""
    patch = RunCustody(run_id="run-p", task_id="task-owner")
    patch.patch_captured = True
    patch.target_root = str(target_root)
    import ouroboros.delegate_custody as custody

    monkeypatch.setattr(custody, "settled_unread_outputs", lambda root, *_a, **_k: [])
    monkeypatch.setattr(custody, "undisposed_patches", lambda root, *_a, **_k: [patch])


def _patch_row(text):
    return next(line for line in text.splitlines()
                if "ANOTHER TASK'S DELEGATED WORK" in line and "patches awaiting apply/reject" in line)


def test_reader_whose_active_root_is_the_target_gets_the_concrete_call(tmp_path, monkeypatch):
    target = tmp_path / "target"
    _foreign_patch_targeting(monkeypatch, target)
    text = context_health.build_health_invariants(
        _env(tmp_path), task_id="task-other", active_root=str(target))
    # The static rule still leads; the call FOLLOWS it.
    assert "apply them from the recorded target" in text
    assert "Your active root matches the target of run-p" in text
    assert "integrate_delegated_patch(run_id='run-p', decision='apply'|'reject')" in text


def test_reader_whose_active_root_contains_a_host_minted_target_gets_the_call(tmp_path, monkeypatch):
    """The aggregator shape: the target is a clone NESTED in the reader's own
    root, both under the host-minted subagent-projects root."""
    projects_root = tmp_path / "projects"
    parent = projects_root / "P"
    target = parent / "contributions" / "tools"
    target.mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROJECTS_ROOT", str(projects_root))
    _foreign_patch_targeting(monkeypatch, target)
    text = context_health.build_health_invariants(
        _env(tmp_path), task_id="task-other", active_root=str(parent))
    assert "integrate_delegated_patch(run_id='run-p', decision='apply'|'reject')" in text


def test_an_unrelated_active_root_keeps_the_static_row_byte_for_byte(tmp_path, monkeypatch):
    target = tmp_path / "target"
    _foreign_patch_targeting(monkeypatch, target)
    static = _patch_row(context_health.build_health_invariants(
        _env(tmp_path), task_id="task-other"))
    unrelated = _patch_row(context_health.build_health_invariants(
        _env(tmp_path), task_id="task-other", active_root=str(tmp_path / "elsewhere")))
    assert unrelated == static
    assert "integrate_delegated_patch(run_id='run-p'" not in unrelated
