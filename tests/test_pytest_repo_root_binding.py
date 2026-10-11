"""The pytest session must not resolve the update-tx marker through the LIVE repo.

Unbound, ``git_ops.REPO_DIR`` sends ``update_merge._update_tx_marker_path()`` at the
operator's real ``~/Ouroboros/repo/.git``, so a staged managed merge makes the registry
guard block the whole suite.
"""

import os
import pathlib

import pytest

import ouroboros.config as config
import supervisor.git_ops as git_ops
import supervisor.update_merge as update_merge


_LIVE_REPO = (pathlib.Path.home() / "Ouroboros" / "repo").resolve(strict=False)

# OUROBOROS_ALLOW_LIVE_REPO_TESTS=1 is the supported opt-in that deliberately KEEPS the live
# repo root (git_ops's own destructive-git fuse reads the same switch), so these tests pin
# behaviour that mode turns off by design.
_LIVE_REPO_OPT_IN = os.environ.get("OUROBOROS_ALLOW_LIVE_REPO_TESTS") == "1"
pytestmark = pytest.mark.skipif(_LIVE_REPO_OPT_IN, reason="live-repo opt-in keeps the live root")


def test_pytest_session_repo_root_is_not_the_live_repo():
    """Load-bearing: pins the conftest binding itself, independent of live state.

    Holds in BOTH modes — the repo binding is keyed on the repo opt-in, not the data one, so a
    live-DATA run still gets a throwaway repo root (its path is then a temp dir, not DATA_DIR).
    """
    bound = git_ops.REPO_DIR.resolve(strict=False)
    assert bound != _LIVE_REPO
    if os.environ.get("OUROBOROS_ALLOW_LIVE_DATA_TESTS") != "1":
        assert bound == (config.DATA_DIR / "repo").resolve(strict=False)
    marker = update_merge._update_tx_marker_path().resolve(strict=False)
    assert marker == bound / ".git" / "ouroboros-update-tx.json"


def test_ambient_tx_read_is_absent_in_pytest():
    """The ambient (un-monkeypatched) strict read is the honest ``absent`` allow.

    NOTE: with no live marker staged this also passes on the unfixed tree — it is a
    behavioural statement, not evidence. The test above is the load-bearing one.
    """
    from ouroboros.tools.registry import _managed_update_code_tool_block

    assert update_merge.read_update_tx_strict()[0] == "absent"
    ctx = type("Ctx", (), {"task_id": "some-task", "task_metadata": None})()
    assert _managed_update_code_tool_block(ctx, "write_file") == ""


def test_guard_still_blocks_an_unauthorized_task_under_the_test_root(tmp_path, monkeypatch):
    """The rebind must not weaken the gate: a real staged tx still blocks everyone else."""
    from ouroboros.tools.registry import _managed_update_code_tool_block

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    tx = {"phase": "assisted_resolution", "task_id": "owner-task"}
    metadata = {
        "managed_update": {
            "authority_fingerprint": update_merge.assisted_authority_fingerprint(tx),
        }
    }
    update_merge.write_update_tx(tx)

    other = type("Ctx", (), {"task_id": "other-task", "task_metadata": metadata})()
    assert "MANAGED_UPDATE_IN_PROGRESS" in _managed_update_code_tool_block(other, "write_file")

    owner = type("Ctx", (), {"task_id": "owner-task", "task_metadata": metadata})()
    assert _managed_update_code_tool_block(owner, "write_file") == ""
