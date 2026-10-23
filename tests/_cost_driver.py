"""Boot the real server over a scale root in THIS fresh interpreter and measure every boundary.

Run by tests/test_cost_contract.py as ``python -m tests._cost_driver SPEC.json OUT.json`` with
``OUROBOROS_DATA_DIR`` naming the root that tests/_scale_root.py built. The meter is installed
before production code is imported, so a measurement is cold by construction: nothing a
previous request, boot or test cached in memory is present.

Only what would leave the machine or start real work is replaced: the provider check says a
provider is ready (no model is called; a call would be recorded and refused), workers are not
spawned, the supervisor's git bootstrap (checkout reset + import-test child process) and the
boot-time update check do not run, the owned Claudexor daemon is not warmed, the starter-skill
installer does not run (the boot of this fresh root with a ready provider schedules it; it would
clone the skill sources from GitHub and install skills while the boundaries are measured; the
request is recorded as ``starter_requests``), and the host service listens on an ephemeral
loopback port. Everything else — the ASGI lifespan, the store import, the supervisor's init, the
gateway handlers, the WebSocket chat door and the Stop path — is production code.

Boundaries (tags in the output):

- ``B1``: the lifespan up to ``lifespan.startup.complete`` (the work ``/api/health`` waits for);
  ``B1h``: the first ``/api/health`` request; ``popen_before_health``: child processes on that
  path (the lifespan and the request); ``B1c``: the boot coordinator thread that runs the remaining
  boot steps after the HTTP yield, when the tree has one (joined once readiness is known);
- ``B2``: the threads that publish the store and initialize the supervisor (``thread_rules``) and
  every step that waits on the store's publication (``store_switch.when_ready``) wherever it runs,
  snapshotted when the chat bus is initialized (``B2_dialogue``) and when the supervisor reports
  ready (``B2_ready``); the supervisor is held there, before its first tick, while the requests
  below are measured. ``B2_child_headers`` separately counts the boot's required directory/header
  compatibility pass; both publication snapshots retain it for its own bounded-work guard;
- ``B4``/``B4w``: an owner chat frame over ``/ws`` on the cold process, then a second one;
- ``B3``/``B3w``: ``GET /api/state``, cold then warm;
- ``R_active``/``R_late`` (+``w``): ``GET /api/chat/history`` of the active and the late-bound room;
- ``B5``: ``POST /api/tasks/<root>/cancel`` with ``{"cascade": true}`` on the pending tree.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from tests._cost_meter import BOUNDARY, CostMeter
from tests._scale_root import CURRENT

READY_TIMEOUT_SEC = 240.0  # the transport bound of this measurement, not a judgment about the boot
STATEMENT_SAMPLE = 80


def _summary(meter: CostMeter, tag: str) -> Dict[str, Any]:
    snap = meter.snapshot(tag)
    statements = sorted(snap["statements"].items(), key=lambda item: -item[1])[:STATEMENT_SAMPLE]
    return {"counts": snap["counts"], "paths": snap["paths"], "names": snap["names"],
            "statements": dict(statements), "callers": snap["callers"]}


def _all_counts(meter: CostMeter) -> Dict[str, Dict[str, int]]:
    return {tag: meter.snapshot(tag)["counts"] for tag in meter.tags()}


class _Tagger:
    """ASGI wrapper: the lifespan runs as ``B1``; a request names its boundary in a header."""

    def __init__(self, app: Any, meter: CostMeter, result: Dict[str, Any]) -> None:
        self.app, self.meter, self.result = app, meter, result

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # Work-count fixture, not a contention benchmark: consecutive requests
            # must share the per-thread connection cache for cold/warm to be stable.
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
            token = BOUNDARY.set("B1")

            async def tagged_send(message):
                if message.get("type") == "lifespan.startup.complete":
                    _mark(self.result, "lifespan_complete")
                    self.result["B1"] = _summary(self.meter, "B1")
                    self.result["process_at_B1"] = _all_counts(self.meter)
                return await send(message)

            try:
                return await self.app(scope, receive, tagged_send)
            finally:
                BOUNDARY.reset(token)
        headers = dict(scope.get("headers") or [])
        tag = headers.get(b"x-cost-boundary", b"").decode() or None
        if tag is None:
            return await self.app(scope, receive, send)
        token = BOUNDARY.set(tag)
        try:
            return await self.app(scope, receive, send)
        finally:
            BOUNDARY.reset(token)


def _stub_external_effects(srv: Any, result: Dict[str, Any]) -> None:
    from ouroboros import claudexor_daemon, llm, skill_starter
    from supervisor import workers

    srv.has_startup_ready_provider = lambda _settings: True
    srv._bootstrap_supervisor_repo = lambda _settings, git_ops_module=None: (True, "cost contract: no git bootstrap")
    srv._boot_managed_update_tasks = lambda: None
    workers.spawn_workers = lambda *_a, **_k: None
    claudexor_daemon.warm_owned_daemon = lambda *_a, **_k: None
    skill_starter.schedule_starter = lambda _root, _ids, *, origin, repo_dir=None: (
        result.setdefault("starter_requests", []).append(origin))

    def refuse_model(*_args, **_kwargs):
        result.setdefault("model_calls", []).append(BOUNDARY.get() or threading.current_thread().name)
        raise RuntimeError("cost contract: no model call is expected on these boundaries")

    llm.LLMClient.chat = refuse_model
    llm.LLMClient.chat_async = refuse_model


def _mark(result: Dict[str, Any], name: str) -> None:
    result.setdefault("marks", {})[name] = round(time.monotonic() - result["_started"], 3)


def _install_checkpoints(meter: CostMeter, srv: Any, result: Dict[str, Any]) -> Dict[str, threading.Event]:
    import functools

    from ouroboros import server_liveness, store_journal, store_switch
    from supervisor import message_bus
    from tests._cost_meter import tagged

    events = {"ready": threading.Event(), "release": threading.Event()}
    when_ready = store_switch.when_ready
    prepare_children = store_journal.prepare_children

    def child_headers(root):
        # Compatibility must precede producers. Measure its directory/header work
        # separately; test_cost_contract bounds it and still forbids history payloads.
        return tagged("B2_child_headers", lambda: prepare_children(root))

    store_journal.prepare_children = child_headers

    def publication_cost():
        return {**_summary(meter, "B2"), "child_headers": _summary(meter, "B2_child_headers")}

    def publication_steps(root, *steps):
        # What waits on the store's publication is B2 wherever it runs: on the import thread, or on
        # the lifespan's own thread when the import already finished (then it also delays health).
        result.setdefault("when_ready_threads", []).append(threading.current_thread().name)
        return when_ready(root, *[functools.partial(tagged, "B2", step) for step in steps])

    store_switch.when_ready = publication_steps
    bus_init = message_bus.init

    def init(*args, **kwargs):
        value = bus_init(*args, **kwargs)
        _mark(result, "dialogue")
        result["B2_dialogue"] = publication_cost()
        result["B2_dialogue_tag"] = BOUNDARY.get() or getattr(threading.current_thread(), "_cost_tag", None)
        return value

    message_bus.init = init
    noted = server_liveness.note_supervisor_ready

    def ready(*args, **kwargs):
        _mark(result, "ready")
        result["B2_ready"] = publication_cost()
        result["B2_ready_tag"] = getattr(threading.current_thread(), "_cost_tag", None)
        result["B2_ready_flag"] = srv._supervisor_ready.is_set()
        events["ready"].set()
        events["release"].wait(READY_TIMEOUT_SEC)  # held before the first tick while requests are measured
        return noted(*args, **kwargs)

    server_liveness.note_supervisor_ready = ready
    return events


def _request(meter: CostMeter, client: Any, result: Dict[str, Any], tag: str, method: str, path: str,
             **kwargs: Any) -> None:
    response = client.request(method, path, headers={"x-cost-boundary": tag}, **kwargs)
    meter.join_spawned(tag, timeout=10.0)
    _mark(result, tag)
    result[tag] = _summary(meter, tag)
    result.setdefault("responses", {})[tag] = {"status": response.status_code, "bytes": len(response.content)}
    if tag in {"B3", "B3w"}:
        payload = response.json()
        result["responses"][tag]["decisions"] = {
            "current_questions": sorted(
                [{key: value for key, value in row.items() if key != "ts"} for row in payload["current_questions"]],
                key=lambda row: (row["task_id"], row["quiz_id"])),
            "current_questions_complete": payload["current_questions_complete"],
            "current_questions_gaps": payload["current_questions_gaps"],
        }


def _chat_frame(meter: CostMeter, client: Any, result: Dict[str, Any], tag: str, message_id: str) -> None:
    frame = {"type": "chat", "content": f"owner message {message_id}", "chat_id": 1,
             "client_message_id": message_id, "sender_session_id": "cost-contract"}
    seen: List[str] = []
    with client.websocket_connect("/ws", headers={"x-cost-boundary": tag}) as socket:
        socket.send_text(json.dumps(frame))
        while True:
            data = json.loads(socket.receive_text())
            seen.append(str(data.get("type") or ""))
            if data.get("client_message_id") == message_id:
                result.setdefault("responses", {})[tag] = {"echo": data.get("system_type") or data.get("role") or "chat",
                                                           "frames": seen}
                break
    meter.join_spawned(tag, timeout=10.0)
    _mark(result, tag)
    result[tag] = _summary(meter, tag)


def run(spec: Dict[str, Any]) -> Dict[str, Any]:
    root = pathlib.Path(spec["root"])
    result: Dict[str, Any] = {"facts": spec.get("facts", {}), "errors": [], "_started": time.monotonic()}
    meter = CostMeter(root, thread_rules=[("current-store-import", "B2"), ("supervisor-main", "B2"),
                                          ("boot-coordinator", "B1c")],
                      lineage={"B1": "B1:spawned", "B2": "B2:spawned", "B1c": "B1c:spawned"})
    meter.__enter__()
    try:
        from starlette.testclient import TestClient

        import server as srv

        _mark(result, "imported")
        _stub_external_effects(srv, result)
        # The install root is resolved once per process, in whichever boundary's thread asks first:
        # a race the history size tips, not history work. Resolve it before any boundary starts.
        from ouroboros import install_identity

        install_identity.canonical_root()
        events = _install_checkpoints(meter, srv, result)
        client = TestClient(_Tagger(srv.app, meter, result))
        with client:
            response = client.get("/api/health", headers={"x-cost-boundary": "B1h"})
            _mark(result, "health")
            result["B1h"] = _summary(meter, "B1h")
            # On the path the first answer waits for: the lifespan up to its yield and the request itself.
            result["popen_before_health"] = (result["B1"]["counts"].get("popen", 0)
                                             + result["B1h"]["counts"].get("popen", 0))
            result.setdefault("responses", {})["B1h"] = {"status": response.status_code}
            if not events["ready"].wait(READY_TIMEOUT_SEC):
                result["errors"].append("the supervisor never reported ready")
            if meter.spawned.get("B1c"):  # the boot coordinator (work after the yield) ends once readiness is known
                if meter.join_spawned("B1c", timeout=READY_TIMEOUT_SEC):
                    result["errors"].append("the boot coordinator did not finish")
                _mark(result, "coordinator")
                result["B1c"] = _summary(meter, "B1c")
            # The one-time archive import runs in the maintenance child on a real install; the
            # boundaries below measure the steady state after it (a room page, an owner message).
            token = BOUNDARY.set("import")
            try:
                from ouroboros.dialogue_import import run_dialogue_import

                result["import"] = {"state": run_dialogue_import(root).get("state")}
            finally:
                BOUNDARY.reset(token)
            try:
                facts = spec.get("facts", {})
                _chat_frame(meter, client, result, "B4", "cost-contract-1")
                _request(meter, client, result, "B3", "GET", "/api/state")
                _request(meter, client, result, "R_active", "GET", f"/api/chat/history?chat_id={facts['active_chat_id']}")
                _request(meter, client, result, "R_late", "GET", f"/api/chat/history?chat_id={facts['late_chat_id']}")
                _chat_frame(meter, client, result, "B4w", "cost-contract-2")
                _request(meter, client, result, "B3w", "GET", "/api/state")
                _request(meter, client, result, "R_activew", "GET", f"/api/chat/history?chat_id={facts['active_chat_id']}")
                _request(meter, client, result, "B5", "POST", f"/api/tasks/{CURRENT['stop_root']}/cancel",
                         json={"cascade": True})
            finally:
                result["process_at_end"] = _all_counts(meter)
                srv._supervisor_stop.set()  # the held supervisor leaves before its first tick
                events["release"].set()
    except Exception:
        result["errors"].append(traceback.format_exc())
    finally:
        meter.__exit__(None, None, None)
    return result


def main(argv: List[str]) -> int:
    spec = json.loads(pathlib.Path(argv[0]).read_text(encoding="utf-8"))
    if pathlib.Path(os.environ.get("OUROBOROS_DATA_DIR", "")).resolve() != pathlib.Path(spec["root"]).resolve():
        raise SystemExit("OUROBOROS_DATA_DIR must name the measured root")
    started = time.monotonic()
    result = run(spec)
    result.pop("_started", None)
    result["driver_seconds"] = round(time.monotonic() - started, 2)
    pathlib.Path(argv[1]).write_text(json.dumps(result, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
