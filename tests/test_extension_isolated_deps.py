"""Split extension-loader regression coverage kept below module size gates."""
from __future__ import annotations

import asyncio
import pathlib
import sys
import threading
import time

import pytest

from ouroboros import extension_loader
from ouroboros.skill_loader import (
    SkillReviewState,
    find_skill,
    save_enabled,
    save_review_state,
)
from tests._shared import clean_extension_runtime_state
from tests.test_extension_loader import (
    _mark_isolated_deps_installed,
    _prepare_extension,
)


@pytest.fixture(autouse=True)
def _clear_loader_state(monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    clean_extension_runtime_state()
    yield
    clean_extension_runtime_state()


def test_load_extension_rejects_outward_symlink_in_skill_tree(tmp_path):
    import os, platform

    if platform.system() == "Windows":
        pytest.skip("symlink creation requires admin on Windows")
    skill_dir = tmp_path / "skills" / "symlinked"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: symlinked\n"
            "description: Symlink escape regression.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: [\"tool\"]\n"
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(
        (
            "from .helper import SECRET\n"
            "def _echo(ctx):\n"
            "    return SECRET\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    save_enabled(drive_root, "symlinked", True)
    loaded = find_skill(drive_root, "symlinked", repo_path=str(skill_dir.parent))
    assert loaded is not None
    save_review_state(
        drive_root,
        "symlinked",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    outside = tmp_path / "outside_helper.py"
    outside.write_text("SECRET = 'escape'\n", encoding="utf-8")
    os.symlink(outside, skill_dir / "helper.py")

    loaded = find_skill(drive_root, "symlinked", repo_path=str(skill_dir.parent))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    assert "symlink" in err.lower()
    assert extension_loader.get_tool(extension_loader.extension_surface_name("symlinked", "echo")) is None


def test_load_extension_ignores_isolated_env_symlinks_when_staging(tmp_path):
    import os
    import platform

    if platform.system() == "Windows":
        pytest.skip("symlink creation requires admin on Windows")
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_symlink",
        (
            "def register(api):\n"
            "    api.register_tool('ok', lambda ctx: 'ok', description='ok', schema={})\n"
        ),
        permissions=["tool"],
    )
    skill_dir = repo_root / "env_symlink"
    bin_dir = skill_dir / ".ouroboros_env" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    os.symlink("/usr/bin/env", bin_dir / "python")

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is None, err
    with extension_loader._lock:
        import_root = pathlib.Path(extension_loader._extensions["env_symlink"].import_root)
    assert not (import_root / "skill" / ".ouroboros_env").exists()
    extension_loader.unload_extension("env_symlink")


def test_load_extension_does_not_import_untracked_isolated_python_deps(tmp_path):

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_untracked",
        (
            "import dummy_untracked_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: dummy_untracked_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
    )
    skill_dir = repo_root / "env_untracked"
    pkg_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "dummy_untracked_pkg"
    )
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'untracked'\n", encoding="utf-8")

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is not None
    assert "dummy_untracked_pkg" in err


def test_load_extension_imports_and_unloads_isolated_python_deps(tmp_path):
    import importlib

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_import",
        (
            "import dummy_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: dummy_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - dummy_pkg\n",
    )
    skill_dir = repo_root / "env_import"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "dummy_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'from-isolated-env'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root, _force_in_process=True)

    assert err is None, err
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("env_import", "value"))
    assert tool is not None
    assert tool["handler"](None) == "from-isolated-env"
    assert str(site_dir.resolve()) not in sys.path

    extension_loader.unload_extension("env_import")

    assert str(site_dir.resolve()) not in sys.path
    assert "dummy_pkg" not in sys.modules
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("dummy_pkg")


def test_load_extension_does_not_execute_isolated_deps_pth_files(tmp_path):

    marker = tmp_path / "pth_executed.txt"
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_pth",
        (
            "import dummy_pth_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: dummy_pth_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - dummy_pth_pkg\n",
    )
    skill_dir = repo_root / "env_pth"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "dummy_pth_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
    (site_dir / "danger.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('boom', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is None, err
    assert not marker.exists()
    extension_loader.unload_extension("env_pth")


def test_isolated_python_deps_do_not_leak_to_other_extensions(tmp_path):

    loaded_a, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_owner",
        (
            "import shared_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: shared_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - shared_pkg\n",
    )
    owner_dir = repo_root / "env_owner"
    site_dir = (
        owner_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "shared_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded_a)

    err_a = extension_loader.load_extension(loaded_a, lambda: {}, drive_root=drive_root, _force_in_process=True)
    assert err_a is None, err_a
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("env_owner", "value"))
    assert tool is not None and tool["handler"](None) == "owned"

    loaded_b, _repo_root, _drive_root = _prepare_extension(
        tmp_path,
        "env_neighbor",
        (
            "import shared_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: shared_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
    )
    err_b = extension_loader.load_extension(loaded_b, lambda: {}, drive_root=drive_root)

    assert err_b is not None
    assert "shared_pkg" in err_b
    assert extension_loader.get_tool(extension_loader.extension_surface_name("env_neighbor", "value")) is None
    extension_loader.unload_extension("env_owner")


def test_isolated_namespace_packages_are_purged_after_import_scope(tmp_path):

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_namespace",
        (
            "import ns_pkg.sub as sub\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: sub.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - ns_pkg\n",
    )
    skill_dir = repo_root / "env_namespace"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    ns_dir = site_dir / "ns_pkg"
    ns_dir.mkdir(parents=True)
    (ns_dir / "sub.py").write_text("VALUE = 'namespace-ok'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root, _force_in_process=True)

    assert err is None, err
    assert "ns_pkg" not in sys.modules
    assert "ns_pkg.sub" not in sys.modules
    extension_loader.unload_extension("env_namespace")


def test_isolated_regular_parent_namespace_child_is_purged_after_import_scope(tmp_path):

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_regular_parent_namespace_child",
        (
            "import importlib\n"
            "importlib.import_module('regular_parent_pkg.data')\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: 'ok', description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - regular_parent_pkg\n",
    )
    skill_dir = repo_root / "env_regular_parent_namespace_child"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "regular_parent_pkg"
    data_dir = pkg_dir / "data"
    data_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'regular-parent'\n", encoding="utf-8")
    (data_dir / "payload.txt").write_text("namespace-child\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root, _force_in_process=True)

    assert err is None, err
    assert "regular_parent_pkg" not in sys.modules
    assert "regular_parent_pkg.data" not in sys.modules
    extension_loader.unload_extension("env_regular_parent_namespace_child")


def test_isolated_regular_parent_namespace_child_is_purged_after_async_handler(tmp_path):
    import sys

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_async_regular_parent_namespace_child",
        (
            "import importlib\n"
            "async def _value(ctx):\n"
            "    module = importlib.import_module('async_regular_parent_pkg.data')\n"
            "    return module.__name__\n"
            "def register(api):\n"
            "    api.register_tool('value', _value, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - async_regular_parent_pkg\n",
    )
    skill_dir = repo_root / "env_async_regular_parent_namespace_child"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "async_regular_parent_pkg"
    data_dir = pkg_dir / "data"
    data_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'regular-parent'\n", encoding="utf-8")
    (data_dir / "payload.txt").write_text("namespace-child\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root, _force_in_process=True)
    assert err is None, err
    tool = extension_loader.get_tool(
        extension_loader.extension_surface_name("env_async_regular_parent_namespace_child", "value")
    )
    assert tool is not None

    assert asyncio.run(tool["handler"]({})) == "async_regular_parent_pkg.data"
    assert "async_regular_parent_pkg" not in sys.modules
    assert "async_regular_parent_pkg.data" not in sys.modules
    extension_loader.unload_extension("env_async_regular_parent_namespace_child")


def test_isolated_site_scope_releases_execution_lock_when_cleanup_fails(tmp_path, monkeypatch):
    from ouroboros import extension_isolated_deps

    def fail_cleanup(_site_dirs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(extension_isolated_deps, "release_isolated_site_dirs", fail_cleanup)

    with extension_isolated_deps.isolated_site_dirs_scope(tmp_path, enabled=False):
        pass

    assert extension_isolated_deps._execution_lock.acquire(blocking=False)
    extension_isolated_deps._execution_lock.release()


def test_async_isolated_site_scope_releases_execution_lock_when_cleanup_fails(tmp_path, monkeypatch):

    from ouroboros import extension_isolated_deps

    def fail_cleanup(_site_dirs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(extension_isolated_deps, "release_isolated_site_dirs", fail_cleanup)

    async def run_scope():
        async with extension_isolated_deps.async_isolated_site_dirs_scope(tmp_path, enabled=False):
            pass

    asyncio.run(run_scope())

    assert extension_isolated_deps._execution_lock.acquire(blocking=False)
    extension_isolated_deps._execution_lock.release()


def test_isolated_site_scope_releases_execution_lock_when_inject_fails(tmp_path, monkeypatch):
    from ouroboros import extension_isolated_deps

    def fail_inject(_skill_dir):
        raise RuntimeError("inject failed")

    monkeypatch.setattr(extension_isolated_deps, "inject_isolated_site_dirs", fail_inject)

    with pytest.raises(RuntimeError, match="inject failed"):
        with extension_isolated_deps.isolated_site_dirs_scope(tmp_path, enabled=True):
            pass

    assert extension_isolated_deps._execution_lock.acquire(blocking=False)
    extension_isolated_deps._execution_lock.release()


def test_async_isolated_site_scope_releases_execution_lock_when_inject_fails(tmp_path, monkeypatch):

    from ouroboros import extension_isolated_deps

    def fail_inject(_skill_dir):
        raise RuntimeError("inject failed")

    monkeypatch.setattr(extension_isolated_deps, "inject_isolated_site_dirs", fail_inject)

    async def run_scope():
        async with extension_isolated_deps.async_isolated_site_dirs_scope(tmp_path, enabled=True):
            pass

    with pytest.raises(RuntimeError, match="inject failed"):
        asyncio.run(run_scope())

    assert extension_isolated_deps._execution_lock.acquire(blocking=False)
    extension_isolated_deps._execution_lock.release()


def test_async_isolated_site_scope_cancel_while_waiting_does_not_wedge_lock(tmp_path):

    from ouroboros import extension_isolated_deps

    async def run_scope():
        async with extension_isolated_deps.async_isolated_site_dirs_scope(tmp_path, enabled=False):
            return "entered"

    async def main():
        assert extension_isolated_deps._execution_lock.acquire(blocking=False)
        task = asyncio.create_task(run_scope())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        extension_isolated_deps._execution_lock.release()
        await asyncio.sleep(0.05)
        assert extension_isolated_deps._execution_lock.acquire(blocking=False)
        extension_isolated_deps._execution_lock.release()

    asyncio.run(main())


def test_no_deps_scopes_overlap_but_dependency_writer_excludes_new_reader(tmp_path):
    """The supported reader/writer topology does not serialize no-deps handlers."""
    from ouroboros import extension_isolated_deps as deps

    entered = threading.Barrier(2)
    release = threading.Event()
    observed = []

    def reader(label):
        with deps.isolated_site_dirs_scope(tmp_path / label, enabled=False):
            observed.append(label)
            entered.wait(timeout=2)
            release.wait(timeout=2)

    first = threading.Thread(target=reader, args=("a",))
    second = threading.Thread(target=reader, args=("b",))
    first.start(); second.start()
    deadline = time.monotonic() + 2
    while len(observed) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(observed) == 2
    assert sorted(observed) == ["a", "b"]
    release.set()
    first.join(timeout=2); second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()

    writer_entered = threading.Event()
    writer_release = threading.Event()
    reader_entered = threading.Event()

    def writer():
        with deps.isolated_site_dirs_scope(tmp_path / "writer", enabled=True):
            writer_entered.set()
            writer_release.wait(timeout=2)

    def later_reader():
        with deps.isolated_site_dirs_scope(tmp_path / "later", enabled=False):
            reader_entered.set()

    wt = threading.Thread(target=writer); wt.start()
    assert writer_entered.wait(timeout=2)
    rt = threading.Thread(target=later_reader); rt.start()
    assert not reader_entered.wait(timeout=0.05)
    writer_release.set()
    assert reader_entered.wait(timeout=2)
    wt.join(timeout=2); rt.join(timeout=2)
    assert not wt.is_alive() and not rt.is_alive()


def test_release_isolated_site_dirs_removes_path_when_module_scan_fails(tmp_path, monkeypatch):
    import types

    from ouroboros import extension_isolated_deps

    skill_dir = tmp_path / "skill"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site_dir.mkdir(parents=True)
    site_str = str(site_dir.resolve())

    injected = extension_isolated_deps.inject_isolated_site_dirs(skill_dir)
    assert injected == [site_str]
    assert site_str in sys.path
    assert extension_isolated_deps._injected_site_dir_refs.get(site_str) == 1
    module_name = "scan_failure_pkg"
    module = types.ModuleType(module_name)
    module.__file__ = str(site_dir / "scan_failure_pkg.py")
    sys.modules[module_name] = module

    def fail_scan(_site_path):
        raise RuntimeError("module scan failed")

    monkeypatch.setattr(extension_isolated_deps, "_module_names_under_site_dir", fail_scan)

    extension_isolated_deps.release_isolated_site_dirs(injected)

    assert site_str not in sys.path
    assert site_str not in extension_isolated_deps._injected_site_dir_refs
    assert module_name not in sys.modules


def test_release_isolated_site_dirs_removes_preexisting_env_parent_path(tmp_path):

    from ouroboros import extension_isolated_deps

    skill_dir = tmp_path / "skill"
    env_parent = skill_dir / ".ouroboros_env" / "python"
    site_dir = (
        env_parent
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site_dir.mkdir(parents=True)
    env_parent_str = str(env_parent.resolve())
    site_str = str(site_dir.resolve())
    sys.path.insert(0, env_parent_str)
    try:
        injected = extension_isolated_deps.inject_isolated_site_dirs(skill_dir)
        assert injected == [site_str]
        extension_isolated_deps.release_isolated_site_dirs(injected)
        assert env_parent_str not in sys.path
        assert site_str not in sys.path
    finally:
        while env_parent_str in sys.path:
            sys.path.remove(env_parent_str)
        while site_str in sys.path:
            sys.path.remove(site_str)
        extension_isolated_deps._injected_site_dir_refs.pop(site_str, None)


def test_inject_isolated_site_dirs_tracks_preexisting_env_path(tmp_path):

    from ouroboros import extension_isolated_deps

    skill_dir = tmp_path / "skill"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site_dir.mkdir(parents=True)
    site_str = str(site_dir.resolve())
    sys.path.insert(0, site_str)
    try:
        injected = extension_isolated_deps.inject_isolated_site_dirs(skill_dir)
        assert injected == [site_str]
        assert extension_isolated_deps._injected_site_dir_refs.get(site_str) == 1
        extension_isolated_deps.release_isolated_site_dirs(injected)
        assert site_str not in sys.path
        assert site_str not in extension_isolated_deps._injected_site_dir_refs
    finally:
        while site_str in sys.path:
            sys.path.remove(site_str)
        extension_isolated_deps._injected_site_dir_refs.pop(site_str, None)


def test_isolated_python_deps_do_not_leak_during_overlapping_handlers(tmp_path):

    started = threading.Event()
    release = threading.Event()
    loaded_a, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_overlap_owner",
        (
            "def _slow(ctx):\n"
            "    ctx['started'].set()\n"
            "    ctx['release'].wait(2)\n"
            "    import overlap_pkg\n"
            "    return overlap_pkg.VALUE\n"
            "def register(api):\n"
            "    api.register_tool('slow', _slow, description='slow', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - overlap_pkg\n",
    )
    owner_dir = repo_root / "env_overlap_owner"
    site_dir = (
        owner_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "overlap_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'owner'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded_a)
    err_a = extension_loader.load_extension(loaded_a, lambda: {}, drive_root=drive_root, _force_in_process=True)
    assert err_a is None, err_a
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("env_overlap_owner", "slow"))
    assert tool is not None

    loaded_b, _repo_root, _drive_root = _prepare_extension(
        tmp_path,
        "env_overlap_neighbor",
        (
            "import overlap_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: overlap_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
    )
    slow_result = {}
    neighbor_result = {}

    def run_slow():
        slow_result["value"] = tool["handler"]({"started": started, "release": release})

    def load_neighbor():
        neighbor_result["err"] = extension_loader.load_extension(loaded_b, lambda: {}, drive_root=drive_root, _force_in_process=True)

    t1 = threading.Thread(target=run_slow)
    t1.start()
    assert started.wait(2)
    t2 = threading.Thread(target=load_neighbor)
    t2.start()
    t2.join(timeout=0.1)
    assert t2.is_alive()
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert slow_result["value"] == "owner"
    assert neighbor_result["err"] is not None
    assert "overlap_pkg" in neighbor_result["err"]
    extension_loader.unload_extension("env_overlap_owner")


def test_isolated_python_deps_do_not_leak_during_overlapping_async_handlers(tmp_path):
    import sys

    loaded_a, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_async_owner",
        (
            "async def _slow(ctx):\n"
            "    ctx['started'].set()\n"
            "    await ctx['release'].wait()\n"
            "    import async_pkg\n"
            "    return async_pkg.VALUE\n"
            "def register(api):\n"
            "    api.register_tool('slow', _slow, description='slow', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - async_pkg\n",
    )
    owner_dir = repo_root / "env_async_owner"
    site_dir = (
        owner_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "async_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'async-owner'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded_a)

    loaded_b, _repo_root, _drive_root = _prepare_extension(
        tmp_path,
        "env_async_neighbor",
        (
            "async def _value(ctx):\n"
            "    import async_pkg\n"
            "    return async_pkg.VALUE\n"
            "def register(api):\n"
            "    api.register_tool('value', _value, description='value', schema={})\n"
        ),
        permissions=["tool"],
    )
    assert extension_loader.load_extension(loaded_a, lambda: {}, drive_root=drive_root, _force_in_process=True) is None
    assert extension_loader.load_extension(loaded_b, lambda: {}, drive_root=drive_root, _force_in_process=True) is None
    tool_a = extension_loader.get_tool(extension_loader.extension_surface_name("env_async_owner", "slow"))
    tool_b = extension_loader.get_tool(extension_loader.extension_surface_name("env_async_neighbor", "value"))

    async def main():
        started = asyncio.Event()
        release = asyncio.Event()
        task_a = asyncio.create_task(tool_a["handler"]({"started": started, "release": release}))
        await asyncio.wait_for(started.wait(), timeout=2)
        task_b = asyncio.create_task(tool_b["handler"]({}))
        await asyncio.sleep(0.05)
        assert not task_b.done()
        release.set()
        assert await asyncio.wait_for(task_a, timeout=2) == "async-owner"
        with pytest.raises(ModuleNotFoundError):
            await asyncio.wait_for(task_b, timeout=2)

    asyncio.run(main())
    extension_loader.unload_extension("env_async_owner")
    extension_loader.unload_extension("env_async_neighbor")
