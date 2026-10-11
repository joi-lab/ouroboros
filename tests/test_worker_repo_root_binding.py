"""A spawned worker must gate on the repo it serves, not on git_ops' hardcoded default.

``git_ops.REPO_DIR`` has no env fallback and ``git_ops.init()`` is never called at boot, so a
spawn-started worker re-imports the module and inherits ``~/Ouroboros/repo``. Everything that
resolves through ``git_ops._git_dir()`` — notably the managed-update transaction marker the
repo-write tool gate reads — would then consult that repo instead of the worker's own.
"""

import pathlib

import supervisor.git_ops as git_ops
import supervisor.update_merge as update_merge
import supervisor.workers as workers


def test_worker_binds_the_repo_root_it_was_given(tmp_path, monkeypatch):
    """The load-bearing assertion: the binding follows the worker's argument."""
    served = tmp_path / "served-repo"
    (served / ".git").mkdir(parents=True)
    monkeypatch.setattr(git_ops, "REPO_DIR", pathlib.Path.home() / "Ouroboros" / "repo")

    workers._bind_worker_repo_root(str(served), str(tmp_path / "data"))

    assert git_ops.REPO_DIR == served
    marker = update_merge._update_tx_marker_path()
    assert marker == served / ".git" / "ouroboros-update-tx.json"


def test_worker_binding_moves_both_roots_and_nothing_else(tmp_path, monkeypatch):
    """Scope pin: the two roots the worker IS given move; the branch names and remote it is NOT
    given keep whatever the child imported — git_ops.init() would overwrite them with defaults
    and silently retarget an install whose branches differ."""
    monkeypatch.setattr(git_ops, "REPO_DIR", pathlib.Path("/sentinel/repo"))
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", pathlib.Path("/sentinel/drive"))
    monkeypatch.setattr(git_ops, "BRANCH_DEV", "sentinel-dev")
    monkeypatch.setattr(git_ops, "REMOTE_URL", "sentinel-remote")

    workers._bind_worker_repo_root(str(tmp_path / "served"), str(tmp_path / "data"))

    assert git_ops.REPO_DIR == tmp_path / "served"
    assert git_ops.DRIVE_ROOT == tmp_path / "data"
    assert git_ops.BRANCH_DEV == "sentinel-dev"
    assert git_ops.REMOTE_URL == "sentinel-remote"


def test_worker_main_binds_before_it_can_read_a_transaction():
    """Ordering pin. The bind must precede every import that resolves the marker; a bind placed
    after the agent/update_merge imports would leave the very window this test exists to close."""
    import inspect

    source = inspect.getsource(workers.worker_main)
    bind_at = source.index("_bind_worker_repo_root(repo_dir, drive_root)")
    for later in ("_prepare_worker_task_runtime()", "make_agent("):
        assert bind_at < source.index(later), f"{later} runs before the repo-root bind"


def test_the_root_handed_to_spawned_children_is_isolated():
    """spawn_workers passes ``str(workers.REPO_DIR)`` to every child (workers.py:1742,1946) and
    the child binds git_ops to it, so an un-isolated value here would send workers started BY A
    TEST back at the operator's checkout — and on fork it would overwrite the inherited binding.
    Fails against the pre-fix tree: workers.REPO_DIR was the live path."""
    live = (pathlib.Path.home() / "Ouroboros" / "repo").resolve(strict=False)
    handed_to_children = pathlib.Path(str(workers.REPO_DIR)).resolve(strict=False)
    assert handed_to_children != live
    assert handed_to_children == git_ops.REPO_DIR.resolve(strict=False)
