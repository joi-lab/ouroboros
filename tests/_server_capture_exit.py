"""Subprocess instrumentation for test_server_capture_exit; never replaces main.

The providerless server runs its real executable, lifespan and owned stop. Only
engine transport and optional background services are substituted. A ready boot
starts the native review pruner (normally a supervisor duty) and a ledgered child.
Capture gates and teardown observations leave the original functions intact.
"""
from contextlib import contextmanager
import functools
from importlib.metadata import EntryPoint
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def invoke(entrypoint, args):
    """Use the declared console callable, just as a generated script does."""
    if entrypoint == "script":
        sys.argv = [str(REPO / "server.py"), *args]
        return runpy.run_path(sys.argv[0], run_name="__main__")
    if entrypoint == "imported":
        import server
        sys.argv = ["server.py", *args]
        return server.main()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["scripts"]
    name = "ouroboros-web" if entrypoint == "console" else "ouroboros"
    sys.argv = [name, *(["server"] if entrypoint == "cli" else []), *args]
    return EntryPoint(name, scripts[name], "console_scripts").load()()


def main():
    scratch, generation = Path(sys.argv[1]), sys.argv[2]
    entrypoint = sys.argv[3] if len(sys.argv) > 3 else "script"
    gates = scratch / generation
    gates.mkdir(exist_ok=True)
    root = scratch / "data"
    from ouroboros import claudexor_daemon, headless, owned_shutdown, process_custody
    from ouroboros import review_checking, server_boot, server_entrypoint, server_process, server_restart
    from ouroboros import server_maintenance
    from tests.test_recovered_capture import Engine

    def record(name, **facts):
        row = {"pid": os.getpid(), "monotonic": time.monotonic(), **facts}
        (gates / (name + ".json")).write_text(json.dumps(row), encoding="utf-8")
        print(name, json.dumps(row), flush=True)

    engine = Engine()
    claudexor_daemon.ensure_owned_gateway = lambda **kw: engine
    claudexor_daemon.read_owned_gateway = lambda **kw: engine
    server_boot.start_background_services = lambda *a, **kw: None

    def gate(kind, action):
        @functools.wraps(action)
        def capture(*args, **kwargs):
            record(kind + "-entered")
            deadline = time.monotonic() + 90
            while not (gates / "release").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("bounded capture gate expired")
                time.sleep(0.02)
            result = action(*args, **kwargs)
            record(kind + "-returned")
            return result
        return capture

    headless.write_workspace_patch_artifacts = gate("ordinary", headless.write_workspace_patch_artifacts)
    review_checking._git_delta = gate("review", review_checking._git_delta)

    children = []
    ready = server_boot.wait_supervisor_outcome

    def after_ready(*args, **kwargs):
        ready(*args, **kwargs)
        child = process_custody.spawn_supervised(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            drive_root=root, purpose="service:exit-regression", scope="session", owner_task_id="exit-test",
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        children.append(child)
        record("ready", child_pid=child.pid)
        threading.Thread(target=server_maintenance._prune_review_checkouts,
                         name="test-review-prune", daemon=True).start()

    server_boot.wait_supervisor_outcome = after_ready
    begin, stop = owned_shutdown.begin_owned_stop, owned_shutdown.stop_owned_work

    def beginning(*args, **kwargs):
        result = begin(*args, **kwargs)
        record("stop-began", records=owned_shutdown.owned_records(root))
        return result

    def stopped(*args, **kwargs):
        result = stop(*args, **kwargs)
        record("owned-stop", outcome=result,
               children=[{"pid": child.pid, "code": child.wait(timeout=2)} for child in children],
               records=owned_shutdown.owned_records(root))
        return result

    owned_shutdown.begin_owned_stop, owned_shutdown.stop_owned_work = beginning, stopped
    bus_stop = server_restart._shutdown_supervisor_event_bus

    def bus_stopped():
        bus_stop()
        record("lifespan-finished")

    server_restart._shutdown_supervisor_event_bus = bus_stopped
    run = server_process._SignalStopServer.run

    def ran(self, *args, **kwargs):
        try:
            if early := os.environ.get("OUROBOROS_TEST_EARLY_SIGNAL"):
                import signal
                signal.raise_signal(getattr(signal, early))
                record("early-signal")
            return run(self, *args, **kwargs)
        finally:
            record("server-finished")

    server_process._SignalStopServer.run = ran
    bind = server_entrypoint.bound_service_socket

    @contextmanager
    def bound(root, kind, *args, **kwargs):
        listener = None
        try:
            with bind(root, kind, *args, **kwargs) as listener:
                yield listener
        finally:
            record(kind + "-socket-closed", closed=listener is not None and listener.fileno() == -1)
            if stream := os.environ.get("OUROBOROS_TEST_BROKEN_STREAM"):
                getattr(sys, stream).close()  # After real teardown, before command-boundary flushing.

    server_entrypoint.bound_service_socket = bound
    # The CLI treats --port 0 as absent; its environment default still supports port 0.
    os.environ["OUROBOROS_SERVER_PORT"] = "0"
    result = invoke(entrypoint, ["--host", "127.0.0.1", "--port", "0"])
    record("main-returned", result=result)
    raise SystemExit(result)


if __name__ == "__main__":
    main()
