"""Helpers shared by the workspace-executor suites.

The git-repo builder was split verbatim out of ``tests/test_workspace_executor.py``
when that module was divided by theme, so every sibling suite keeps the exact
repository layout it was written against. ``fake_docker_cli`` is the one Docker CLI
boundary those suites fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def fake_docker_cli(monkeypatch, script) -> None:
    """Answer every ``docker ...`` process from ``script(cmd, **kwargs)`` -> CompletedProcess.

    The producer runs short CLI commands with ``subprocess.run`` and submits a
    service start with ``subprocess.Popen`` under the owner Pause gate
    (``workspace_executor._submit_service_command``). Both reach the same script,
    so no CLI or daemon is contacted while the real submission path still runs;
    any other argv keeps the real ``Popen``.
    """
    real_popen = subprocess.Popen

    class DockerCli:
        def __init__(self, cmd, **kwargs):
            self.args = cmd
            answer = script(cmd, **kwargs)
            self.returncode, self._output = answer.returncode, (answer.stdout, answer.stderr)

        def communicate(self, timeout=None):
            return self._output

    def popen(cmd, *args, **kwargs):
        if list(cmd[:1]) == ["docker"]:
            return DockerCli(cmd, **kwargs)
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", script)
    monkeypatch.setattr(subprocess, "Popen", popen)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
