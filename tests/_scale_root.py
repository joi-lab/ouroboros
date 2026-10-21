"""A published data root whose current facts are constant and whose unrelated history scales with ``k``.

The cost contract (tests/test_cost_contract.py) boots and queries two such roots, ``k=1`` and
``k=30``, and requires every public boundary to do the same work on both. ``build(root, k)``
must therefore keep everything a boundary may legitimately read identical for every ``k``
(ids, sizes, timestamps of the current set) and grow only history nothing interactive needs.

Current facts (constant): three running tasks (a root with a child, and a task of the active
project) as the previous process left them in the queue snapshot; a pending tree of two tasks
(``CURRENT["stop_root"]``, the tree the Stop boundary stops); a paused root whose owner fence is
closed and which holds one open owner question; Main's own recent rows; two project rooms: the
active one with its rows in the OLDEST archive segment, and one bound later to an old task
(``CURRENT["late_task"]``) whose rows are just as old. A finished author keeps one expired,
still-answerable question; its already-delivered result grows with ``k`` while its question
stays fixed, so reading the full body for that question cannot pass the state contract.

Unrelated history (per unit of ``k``): ``LEGACY_PER_K`` frozen ``task_results/*.json`` files
imported at the cutover by the real boot preparation (``startup_migrations.prepare_startup_state``,
which also stamps the one-time migrations as a previous boot did), ``SETTLED_PER_K`` settled tasks with 20-200 KB bodies written through
the store, ``CHILDREN_PER_K`` child drives under ``state/headless_tasks/`` with their own
``current.sqlite``, archive segments of chat/progress/tools/events (``SEGMENTS_PER_K``) whose
rows belong to OTHER rooms and are NEWER than the requested room's rows (a LIMIT cannot hide a
walk), ``CUSTODY_PER_K`` closed delegated-run custody records, one settled task per unit that
still carries an answered, fully delivered owner question. Undelivered answers are CURRENT
facts under ``quiz_needs_retention`` and must not scale with history. ``NEVER_ACTIVE_PROJECTS``
stays constant: a registered project is a current fact (every ``/api/state`` lists it), and one
project that never had a row already sends a boot through the whole growing archive.

Run in its own interpreter with ``OUROBOROS_DATA_DIR`` set to the root (the writers that use
the process data root then write there): ``python -m tests._scale_root ROOT K OUT.json``.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import time
from typing import Any, Dict, List

LEGACY_PER_K = 10
SETTLED_PER_K = 10
CHILDREN_PER_K = 3
CUSTODY_PER_K = 5
SEGMENTS_PER_K = {"chat": 1, "progress": 2, "tools": 2, "events": 2}
ROWS_PER_SEGMENT = 150
# A registered project is a current fact (the sidebar lists it on every /api/state), so their
# number stays constant; one never-active project already sends a boot through the archive.
NEVER_ACTIVE_PROJECTS = 1
BODY_SIZES = (20_000, 40_000, 80_000, 200_000)
MAIN = 1
FOREIGN_CHATS = (MAIN, 424242, 515151)  # Main and two transport mirrors: never a project room
OLD = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
NEW = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)

CURRENT: Dict[str, Any] = {
    "running": ("cur-run-root", "cur-run-child", "cur-run-p1"),
    "stop_root": "cur-pend-root",
    "pending": ("cur-pend-root", "cur-pend-child"),
    "paused_root": "cur-paused-root",
    "question": ("cur-paused-root", "quiz-open-1"),
    "retained_question": ("cur-finished-author", "quiz-expired-1"),
    "active_project": "p-active",
    "late_project": "p-late",
    "late_task": "hist-late-0",
    "main_rows": 6,
    "room_rows": 24,
}


def _iso(moment: datetime.datetime) -> str:
    return moment.isoformat()


def _stamp(moment: datetime.datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S")


def _body(size: int, tag: str) -> str:
    unit = f"{tag} result line with ordinary prose about the finished work. "
    return (unit * (size // len(unit) + 1))[:size]


def _write_segment(root: pathlib.Path, prefix: str, moment: datetime.datetime, rows: List[dict]) -> pathlib.Path:
    path = root / "archive" / f"{prefix}_{_stamp(moment)}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def _chat_row(moment: datetime.datetime, chat_id: int, index: int, *, task_id: str = "", inbound: bool) -> dict:
    return {"ts": _iso(moment), "session_id": "fixture", "direction": "in" if inbound else "out",
            "chat_id": chat_id, "user_id": 7 if inbound else 0,
            "text": f"message {index} of room {chat_id} with a few words of ordinary conversation",
            "format": "", "source": "web" if inbound else "", "sender_label": "", "sender_session_id": "",
            "client_message_id": f"cm-{chat_id}-{index}-{_stamp(moment)}" if inbound else "",
            "transport": {}, "task_id": task_id}


def _progress_row(moment: datetime.datetime, chat_id: int, task_id: str, index: int) -> dict:
    text = f"progress {index} of {task_id}: reading files and writing the next step"
    return {"ts": _iso(moment), "type": "send_message", "task_id": task_id, "is_progress": True,
            "direction": "out", "chat_id": chat_id, "user_id": 0, "text": text, "content": text,
            "format": "", "role": "assistant", "system_type": "", "narration": False}


def _history_rows(prefix: str, moment: datetime.datetime, unit: int, part: int) -> List[dict]:
    rows = []
    for index in range(ROWS_PER_SEGMENT):
        at = moment + datetime.timedelta(seconds=index)
        chat_id = FOREIGN_CHATS[index % len(FOREIGN_CHATS)]
        task_id = f"hist-settled-{unit}-{index % SETTLED_PER_K}"
        if prefix == "chat":
            rows.append(_chat_row(at, chat_id, index, inbound=index % 2 == 0))
        elif prefix == "progress":
            rows.append(_progress_row(at, chat_id, task_id, index))
        elif prefix == "tools":
            rows.append({"ts": _iso(at), "task_id": task_id, "tool": "read_file",
                         "args": {"path": f"src/module_{index}.py"}, "result_preview": "x" * 120})
        elif index < 2 * CUSTODY_PER_K and part == 0:
            kind = "delegate_run_started" if index % 2 == 0 else "delegate_run_settled"
            rows.append({"ts": _iso(at), "type": kind, "event_id": f"ev-{unit}-{index}",
                         "run_id": f"run-hist-{unit}-{index // 2}", "task_id": task_id,
                         **({"state": "succeeded"} if index % 2 else {})})
        else:
            rows.append({"ts": _iso(at), "type": "llm_round", "task_id": task_id, "round": index,
                         "prompt_tokens": 1000 + index, "completion_tokens": 100, "part": part})
    return rows


def _legacy_document(task_id: str, size: int, moment: datetime.datetime) -> dict:
    from ouroboros.task_result_schema import SCHEMA_VERSION_KEY, TASK_RESULT_SCHEMA_VERSION

    return {SCHEMA_VERSION_KEY: TASK_RESULT_SCHEMA_VERSION, "task_id": task_id, "status": "completed",
            "chat_id": FOREIGN_CHATS[len(task_id) % len(FOREIGN_CHATS)], "ts": _iso(moment),
            "updated_at": _iso(moment), "result": _body(size, task_id), "cost_usd": 0.01}


def _legacy_files(root: pathlib.Path, k: int) -> int:
    folder = root / "task_results"
    folder.mkdir(parents=True, exist_ok=True)
    count = 0
    for unit in range(k):
        for index in range(LEGACY_PER_K):
            task_id = f"hist-legacy-{unit}-{index}"
            document = _legacy_document(task_id, BODY_SIZES[index % len(BODY_SIZES)],
                                        OLD + datetime.timedelta(days=unit, minutes=index))
            (folder / f"{task_id}.json").write_text(json.dumps(document), encoding="utf-8")
            count += 1
    return count


def _discharged(root: pathlib.Path, task_id: str) -> Dict[str, Any]:
    """A settled task as history holds it: its terminal projection was published (``attempt``-less
    marker), so it owes no synthesis and no projection; the real writer stores the marker."""
    from ouroboros.task_store import patch_document, read_document

    patch_document(root, task_id, lambda current: {**current, "canonical_terminal_projection": {
        "published_at": current.get("updated_at") or _iso(NEW)}})
    return read_document(root, task_id)


def _settled(root: pathlib.Path, k: int) -> Dict[str, int]:
    """``SETTLED_PER_K`` settled tasks per unit: one template through the real lifecycle writer,
    the rest stored through the store's own transaction with the template's shape (one write
    transaction per unit keeps k=30 fast); child drives are copies of one real child store."""
    import shutil

    from ouroboros import owner_quiz, store
    from ouroboros.task_results import write_task_result

    write_task_result(root, "hist-template", "completed", chat_id=MAIN, result=_body(20_000, "template"),
                      ts=_iso(NEW))
    template = _discharged(root, "hist-template")
    child_template = root / "state" / "headless_tasks" / "hist-template" / "data"
    child_template.mkdir(parents=True, exist_ok=True)
    write_task_result(child_template, "hist-template-sub", "completed", result=_body(20_000, "sub"),
                      parent_task_id="hist-template", root_task_id="hist-template")
    _discharged(child_template, "hist-template-sub")
    children = 1
    for unit in range(k):
        with store.open_store(root, write=True) as txn:
            for index in range(SETTLED_PER_K):
                task_id = f"hist-settled-{unit}-{index}"
                at = _iso(NEW + datetime.timedelta(days=unit, minutes=index))
                txn.store_task({**template, "task_id": task_id, "chat_id": FOREIGN_CHATS[index % len(FOREIGN_CHATS)],
                                "result": _body(BODY_SIZES[index % len(BODY_SIZES)], task_id),
                                "ts": at, "updated_at": at, "canonical_terminal_projection": {"published_at": at}},
                               pending_drive=False)
        for index in range(CHILDREN_PER_K):
            drive = root / "state" / "headless_tasks" / f"hist-settled-{unit}-{index}" / "data"
            shutil.copytree(child_template, drive)
            children += 1
        answered = f"hist-asked-{unit}"
        write_task_result(root, answered, "running", chat_id=MAIN)
        owner_quiz.record_asked(root, answered, quiz_id=f"quiz-{unit}", question="Which one?",
                                options=["A", "B"], chat_id=MAIN)
        owner_quiz.record_publication(root, answered, f"quiz-{unit}", MAIN)
        owner_quiz.record_answered(root, answered, quiz_id=f"quiz-{unit}", option_index=0,
                                   request_id=f"answer-{unit}")
        owner_quiz.mark_discharged(root, answered, f"quiz-{unit}")
        write_task_result(root, answered, "completed", result="Answered and finished.")
        _discharged(root, answered)
    return {"settled": k * (SETTLED_PER_K + 1) + 1, "child_drives": children}


def _custody(root: pathlib.Path, k: int) -> int:
    """Closed delegated-run custody: one run through the real emitter (its closing receipt retired
    as the terminal acknowledgement does), the rest stored as copies of its closed record."""
    from ouroboros import delegate_custody as custody
    from ouroboros import obligations, store

    custody.emit(root, custody.STARTED, {"run_id": "run-hist-template", "task_id": "hist-template"})
    custody.emit(root, custody.SETTLED, {"run_id": "run-hist-template", "task_id": "hist-template",
                                         "state": "succeeded"})
    obligations.remove(root, "delegated_runs", "hist-template")
    count = 0
    with store.open_store(root, write=True) as txn:
        closed = txn.custody_row("run-hist-template")
        for unit in range(k):
            for index in range(CUSTODY_PER_K):
                run_id, task_id = f"run-hist-{unit}-{index}", f"hist-settled-{unit}-{index % SETTLED_PER_K}"
                txn.put_custody(run_id, {**closed, "run_id": run_id, "task_id": task_id}, state="closed")
                count += 1
    return count


def _segments(root: pathlib.Path, k: int) -> Dict[str, int]:
    """Foreign rooms' rows, newer than every row of the requested rooms."""
    written: Dict[str, int] = {}
    for unit in range(k):
        for prefix, per_k in SEGMENTS_PER_K.items():
            for part in range(per_k):
                moment = NEW + datetime.timedelta(days=unit, hours=part * 2 + 1)
                _write_segment(root, prefix, moment, _history_rows(prefix, moment, unit, part))
                written[prefix] = written.get(prefix, 0) + 1
    return written


def _rooms(root: pathlib.Path) -> Dict[str, Any]:
    """The requested rooms' own rows: the oldest segments of the chain (constant for every k)."""
    from ouroboros.contracts.chat_id_policy import project_chat_id

    active_chat = project_chat_id(CURRENT["active_project"])
    late_task = CURRENT["late_task"]
    chat_rows, progress_rows = [], []
    for index in range(CURRENT["room_rows"]):
        at = OLD + datetime.timedelta(minutes=index)
        chat_rows.append(_chat_row(at, active_chat, index, inbound=index % 2 == 0))
        progress_rows.append(_progress_row(at, MAIN, late_task, index))
    _write_segment(root, "chat", OLD, chat_rows)
    _write_segment(root, "progress", OLD, progress_rows)
    return {"active_chat_id": active_chat, "late_chat_id": project_chat_id(CURRENT["late_project"])}


def _projects(root: pathlib.Path, k: int) -> Dict[str, Any]:
    from ouroboros import projects_registry as registry
    from ouroboros.task_results import write_task_result

    registry.create_project(root, CURRENT["active_project"], name="Active project", origin="owner_ui")
    write_task_result(root, CURRENT["late_task"], "completed", chat_id=MAIN, result=_body(20_000, "late"),
                      ts=_iso(OLD))
    _discharged(root, CURRENT["late_task"])
    registry.create_project(root, CURRENT["late_project"], name="Late project", origin="owner_ui")
    registry.bind_task_to_project(root, CURRENT["late_task"], CURRENT["late_project"],
                                  origin={"absent": "post_hoc_unresolved"})
    quiet = NEVER_ACTIVE_PROJECTS
    for index in range(quiet):
        registry.create_project(root, f"p-quiet-{index}", name=f"Quiet {index}")
    return {"never_active_projects": quiet}


def _current(root: pathlib.Path, k: int) -> Dict[str, Any]:
    from ouroboros import budget_pause, owner_pause, owner_quiz
    from ouroboros import projects_registry as registry
    from ouroboros.contracts.chat_id_policy import project_chat_id
    from ouroboros.task_results import write_task_result
    from supervisor import queue, state

    state.update_state(lambda st: st.update(owner_chat_id=MAIN))
    queue.init(root)
    run_root, run_child, run_p1 = CURRENT["running"]
    pend_root, pend_child = CURRENT["pending"]
    tasks = {
        run_root: {"id": run_root, "type": "task", "chat_id": MAIN, "text": "running root", "root_task_id": run_root},
        run_child: {"id": run_child, "type": "task", "chat_id": MAIN, "text": "running child",
                    "parent_task_id": run_root, "root_task_id": run_root, "delegation_role": "subagent", "depth": 1},
        run_p1: {"id": run_p1, "type": "task", "chat_id": project_chat_id(CURRENT["active_project"]),
                 "text": "running in the active project", "root_task_id": run_p1,
                 "project_id": CURRENT["active_project"]},
        pend_root: {"id": pend_root, "type": "task", "chat_id": MAIN, "text": "pending root", "root_task_id": pend_root},
        pend_child: {"id": pend_child, "type": "task", "chat_id": MAIN, "text": "pending child",
                     "parent_task_id": pend_root, "root_task_id": pend_root, "delegation_role": "subagent",
                     "depth": 1},
    }
    registry.bind_task_to_project(root, run_p1, CURRENT["active_project"], origin={"absent": "system"})
    for task_id in CURRENT["running"]:
        task = tasks[task_id]
        write_task_result(root, task_id, "running", chat_id=task["chat_id"], text=task["text"],
                          **{key: task[key] for key in ("parent_task_id", "root_task_id", "project_id") if key in task})
    for task_id in CURRENT["pending"]:
        task = tasks[task_id]
        write_task_result(root, task_id, "scheduled", chat_id=MAIN, text=task["text"],
                          **{key: task[key] for key in ("parent_task_id", "root_task_id") if key in task})
    paused = CURRENT["paused_root"]
    write_task_result(root, paused, "scheduled", chat_id=MAIN, text="paused root", root_task_id=paused)
    fence, _created = owner_pause.install_fence(root, paused, request_id="fixture-pause")
    owner_pause.set_fence_state(root, paused, fence_id=fence["fence_id"], state="paused")
    budget_pause.set_budget_pause(root, paused, {"pause_id": "fixture-pause", "state": "paused",
                                                 "reason": "owner", "rail": owner_pause.RAIL_OWNER_PAUSE})
    asker, quiz_id = CURRENT["question"]
    owner_quiz.record_asked(root, asker, quiz_id=quiz_id, question="Which layout do you prefer?",
                            options=["Rows", "Grid"], chat_id=MAIN, recommended_index=0)
    owner_quiz.record_publication(root, asker, quiz_id, MAIN)
    asker, quiz_id = CURRENT["retained_question"]
    write_task_result(root, asker, "completed", chat_id=MAIN,
                      result=_body(20_000 * k, "already-delivered result"), ts=_iso(OLD))
    _discharged(root, asker)
    owner_quiz.record_asked(root, asker, quiz_id=quiz_id, question="Which follow-up format?",
                            options=["PDF", "HTML"], chat_id=MAIN)
    owner_quiz.record_publication(root, asker, quiz_id, MAIN)
    owner_quiz.mark_published(root, asker, quiz_id)
    owner_quiz.reconcile_terminal(root, asker)
    with queue._queue_lock:
        queue.PENDING[:] = [dict(tasks[pend_root], priority=0, queued_at=time.time()),
                            dict(tasks[pend_child], priority=0, queued_at=time.time())]
        queue.RUNNING.clear()
        for slot, task_id in enumerate(CURRENT["running"], start=1):
            queue.RUNNING[task_id] = {"task": dict(tasks[task_id]), "worker_id": slot, "attempt": 1,
                                      "started_at": time.time()}
    if not queue.persist_queue_snapshot(reason="fixture"):
        raise RuntimeError("the fixture queue snapshot was not published")
    with queue._queue_lock:
        queue.PENDING.clear()
        queue.RUNNING.clear()
    return {"tasks": sorted(tasks), "paused_root": paused}


def _main_rows(root: pathlib.Path) -> None:
    from supervisor import message_bus

    for index in range(CURRENT["main_rows"]):
        message_bus.log_chat("in" if index % 2 == 0 else "out", MAIN, 7 if index % 2 == 0 else 0,
                             f"recent main message {index}", source="web",
                             client_message_id=f"main-recent-{index}" if index % 2 == 0 else "",
                             drive_root=root, require_write=True)


def build(root: Any, k: int) -> Dict[str, Any]:
    """Build the published root (see the module docstring); returns its facts."""
    from ouroboros.startup_migrations import prepare_startup_state
    from supervisor import state

    started = time.monotonic()
    root = pathlib.Path(root)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    state.init(root, 100.0)
    state.init_state()  # a first boot's state, before any history exists
    (root / "settings.json").write_text(json.dumps({"TOTAL_BUDGET": 100, "OUROBOROS_MAX_WORKERS": 2}),
                                        encoding="utf-8")
    facts: Dict[str, Any] = {"k": k}
    steps: Dict[str, float] = {}

    def step(name: str, call):
        begun = time.monotonic()
        value = call()
        steps[name] = round(time.monotonic() - begun, 2)
        return value

    facts["legacy_files"] = step("legacy_files", lambda: _legacy_files(root, k))
    # The real boot preparation (import, publication, migration stamps), as the install's previous boots ran it.
    facts["import"] = step("import", lambda: prepare_startup_state(root)["status"])
    facts.update(step("settled", lambda: _settled(root, k)))
    facts["custody_runs"] = step("custody", lambda: _custody(root, k))
    facts["segments"] = step("segments", lambda: _segments(root, k))
    facts.update(step("rooms", lambda: _rooms(root)))
    facts.update(step("projects", lambda: _projects(root, k)))
    facts.update(step("current", lambda: _current(root, k)))
    step("main_rows", lambda: _main_rows(root))
    facts["build_seconds"] = round(time.monotonic() - started, 2)
    facts["build_steps"] = steps
    return facts


def main(argv: List[str]) -> int:
    root, k, out = pathlib.Path(argv[0]), int(argv[1]), pathlib.Path(argv[2])
    if pathlib.Path(os.environ.get("OUROBOROS_DATA_DIR", "")).resolve() != root.resolve():
        raise SystemExit("OUROBOROS_DATA_DIR must name the root being built")
    facts = build(root, k)
    out.write_text(json.dumps(facts, indent=1, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
