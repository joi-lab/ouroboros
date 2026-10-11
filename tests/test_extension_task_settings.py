"""The real per-call extension child keeps admitted settings and existing grants."""

import json
import pathlib

import pytest

from ouroboros import config, extension_loader
from ouroboros.extension_process_runner import ExtensionProcessError, dispatch_extension_tool_subprocess
from ouroboros.settings_integrity import task_settings_scope, task_settings_snapshot
from ouroboros.skill_loader import find_skill, save_skill_grants
from ouroboros.tools.registry import ToolContext
from tests._extension_loader_shared import (
    _add_fake_native_dep, _mark_isolated_deps_installed, _prepare_extension,
    _clear_loader_state,  # noqa: F401 -- autouse lifecycle fixture
)


def test_real_extension_child_preserves_typed_absent_empty_and_grants(tmp_path, monkeypatch):
    keys = ["OPENAI_API_KEY", "EMPTY_VALUE", "STRUCTURED_VALUE", "LATER_VALUE", "GITHUB_REPO"]
    plugin = (
        "import json, os\n"
        "def register(api):\n"
        f"    keys = {keys!r}\n"
        "    def read(ctx):\n"
        "        return json.dumps({'settings': api.get_settings(keys), 'env_key': os.environ.get('OPENAI_API_KEY')})\n"
        "    api.register_tool('read', read, description='Read granted settings', schema={})\n"
    )
    loaded, skills_root, drive = _prepare_extension(
        tmp_path, "snapshot_plugin", plugin, permissions=["tool", "read_settings"],
        env_from_settings=keys, extra_frontmatter="dependencies:\n  - dummy_pkg\n",
    )
    _add_fake_native_dep(loaded)
    _mark_isolated_deps_installed(drive, loaded)
    loaded = find_skill(drive, loaded.name, repo_path=str(skills_root))
    settings_path = drive / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", settings_path)
    old = {"OPENAI_API_KEY": "test-old", "EMPTY_VALUE": "", "STRUCTURED_VALUE": {"rows": [1]}, "GITHUB_REPO": "old/repo"}
    new = {"OPENAI_API_KEY": "test-new", "EMPTY_VALUE": "new", "STRUCTURED_VALUE": {"rows": [2]}, "LATER_VALUE": "new", "GITHUB_REPO": "new/repo"}
    settings_path.write_text(json.dumps(new))
    save_skill_grants(drive, loaded.name, keys,
        content_hash=loaded.content_hash, requested_keys=keys,
        granted_permissions=[], requested_permissions=[])
    ctx = ToolContext(repo_dir=pathlib.Path(__file__).resolve().parents[1], drive_root=drive)
    with task_settings_scope(task_settings_snapshot(old, {})):
        err = extension_loader.load_extension(loaded, config.load_settings, drive_root=drive)
        assert err is None, err
        tool = extension_loader.get_tool(extension_loader.extension_surface_name(loaded.name, "read"))
        actual = json.loads(dispatch_extension_tool_subprocess(tool, ctx, {}))
        assert actual == {"settings": dict(old, GITHUB_REPO="new/repo"), "env_key": None}
        # Revoking the existing content-bound grant still affects the next call.
        grant_file = drive / "state/skills" / loaded.name / "grants.json"
        grants = json.loads(grant_file.read_text())
        grants["granted_keys"] = []
        grant_file.write_text(json.dumps(grants))
        with pytest.raises(ExtensionProcessError, match="missing owner grants"):
            dispatch_extension_tool_subprocess(tool, ctx, {})
        save_skill_grants(drive, loaded.name, keys, content_hash=loaded.content_hash,
            requested_keys=keys, granted_permissions=[], requested_permissions=[])
    actual = json.loads(dispatch_extension_tool_subprocess(tool, ctx, {}))
    assert actual["settings"]["STRUCTURED_VALUE"] == {"rows": [2]}
    assert actual["settings"]["LATER_VALUE"] == "new"
