"""Port sweeps must target the LISTENER, never connected clients.

Both POSIX sweep helpers (``platform_layer.kill_process_on_port`` and the
launcher's ``_kill_stale_on_port``) select processes with ``lsof``. A bare
``tcp:PORT`` selector also matches ESTABLISHED client sockets — on a
browser-mode install the owner's own browser holds one to the UI port, so an
unscoped sweep SIGKILLs the whole browser at panic/startup. These tests pin
the ``-sTCP:LISTEN`` scoping (parity with the Windows branch's ``LISTENING``
netstat filter) and that only lsof-listed pids are signalled.

The POSIX branch is forced via ``IS_WINDOWS`` monkeypatching so the pins hold
on every CI platform; ``subprocess.run`` and the kill primitives are faked, so
no real process is touched.
"""

import ouroboros.platform_layer as platform_layer


class _FakeCompleted:
    def __init__(self, stdout: str):
        self.stdout = stdout


def test_kill_process_on_port_scopes_lsof_to_listeners(monkeypatch):
    seen: dict = {"argv": None, "killed": []}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return _FakeCompleted("101\n202\n")

    monkeypatch.setattr(platform_layer, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_layer.subprocess, "run", fake_run)
    monkeypatch.setattr(
        platform_layer.os, "kill", lambda pid, sig: seen["killed"].append((pid, sig))
    )

    platform_layer.kill_process_on_port(8765)

    assert seen["argv"] is not None, "POSIX branch must consult lsof"
    assert "-sTCP:LISTEN" in seen["argv"], (
        "unscoped tcp:PORT also matches ESTABLISHED client sockets "
        "(the owner's browser) — the sweep must stay a listener sweep"
    )
    assert "tcp:8765" in seen["argv"]
    assert "-nP" in seen["argv"], "name resolution could eat the 5s timeout"
    assert seen["killed"] == [(101, 9), (202, 9)]


def test_kill_process_on_port_spares_self(monkeypatch):
    killed: list = []

    monkeypatch.setattr(platform_layer, "IS_WINDOWS", False)
    monkeypatch.setattr(
        platform_layer.subprocess,
        "run",
        lambda argv, **kwargs: _FakeCompleted(f"{platform_layer.os.getpid()}\n303\n"),
    )
    monkeypatch.setattr(
        platform_layer.os, "kill", lambda pid, sig: killed.append(pid)
    )

    platform_layer.kill_process_on_port(8765)

    assert killed == [303]


def test_launcher_stale_sweep_scopes_lsof_to_listeners(monkeypatch):
    import launcher

    seen: dict = {"argv": None, "killed": []}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return _FakeCompleted("404\n")

    monkeypatch.setattr(launcher, "IS_WINDOWS", False)
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher, "force_kill_pid", lambda pid: seen["killed"].append(pid))
    # The except-branch fallback must not fire when lsof succeeds.
    monkeypatch.setattr(
        launcher,
        "kill_process_on_port",
        lambda port: (_ for _ in ()).throw(AssertionError("fallback must not run")),
    )

    launcher._kill_stale_on_port(8765)

    assert seen["argv"] is not None
    assert "-sTCP:LISTEN" in seen["argv"]
    assert "tcp:8765" in seen["argv"]
    assert seen["killed"] == [404]
