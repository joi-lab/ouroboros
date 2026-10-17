#!/usr/bin/env python3
"""Standalone Ouroboros planning-organ run: write one plan revision and ask the council.

Drives the SAME organ ``plan_task`` uses (``ouroboros/tools/plan_review.py`` over
``plan_ask`` / ``plan_record``) on an operator-supplied revision — goal, plan prose,
done_when JSON, affected paths, attachments — against the configured reviewer rows, in an
ISOLATED drive root, and prints the recorded plan record (the revision, every participant's
typed state and reply) plus the tool's own text, untruncated. Nothing here is a second
engine. ``--dry-run`` builds every participant's delivery and prints its size and the
participants, then stops BEFORE any paid dispatch.

Usage (from anywhere):
    python scripts/run_plan_review.py --goal "..." --plan /path/to/plan.md \\
        [--done-when-json /path/to/done_when.json] [--affected-path PATH ...] \\
        [--attach path/or/task:<id> ...] [--ask council | --ask seat_id ...] [--message "..."] \\
        [--subject-root /path/to/workspace] [--drive-root /tmp/x] [--dry-run] [--output out.txt]

``done_when.json`` is a JSON array in the acceptance claim grammar: ``[{"id", "claim",
"support"}]`` (``support`` says how the claim will be shown). ``--affected-path`` repeats; the
first revision requires the list (none given = ``[]``, the work changes no files).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _load_settings_into_env() -> None:
    """Load runtime settings through the shared config path; never print secrets."""
    try:
        from ouroboros.config import apply_settings_to_env, load_settings
        from ouroboros.server_runtime import apply_runtime_provider_defaults

        settings, _changed, _changed_keys = apply_runtime_provider_defaults(load_settings())
        apply_settings_to_env(settings)
    except Exception as exc:  # pragma: no cover - operator script
        print(f"WARN: could not load/apply Ouroboros settings: {exc}", file=sys.stderr)


def _read_text_file(path_text: str, *, label: str) -> str:
    path = pathlib.Path(path_text).expanduser().resolve(strict=False)
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        raise SystemExit(f"ERROR: could not read {label} at {path}: {exc}") from exc


def _read_done_when(path_text: str) -> list:
    if not path_text:
        return []
    try:
        payload = json.loads(_read_text_file(path_text, label="done_when JSON"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: done_when JSON is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("ERROR: done_when JSON must be an array of {id, claim, support} objects")
    return payload


def _context(args: argparse.Namespace):
    import tempfile

    from ouroboros.tools.registry import ToolContext

    drive_root = (
        pathlib.Path(args.drive_root).expanduser().resolve(strict=False)
        if args.drive_root
        else pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-plan-review-"))
    )
    drive_root.mkdir(parents=True, exist_ok=True)
    (drive_root / "logs").mkdir(parents=True, exist_ok=True)
    subject_root = (
        pathlib.Path(getattr(args, "subject_root", "")).expanduser().resolve(strict=False)
        if getattr(args, "subject_root", "") else REPO
    )
    task_id = str(getattr(args, "task_id", "") or "plan-review-cli")
    ctx = ToolContext(
        repo_dir=REPO,
        system_repo_dir=REPO,
        workspace_root=subject_root if subject_root != REPO else None,
        workspace_mode="external" if subject_root != REPO else "",
        drive_root=drive_root,
        task_id=task_id,
        task_metadata={"root_task_id": task_id},
    )
    return ctx, drive_root, subject_root, task_id


def _run(args: argparse.Namespace) -> str:
    from ouroboros import plan_record
    from ouroboros.task_results import write_task_result
    from ouroboros.tools import plan_ask, plan_review

    plan = _read_text_file(args.plan, label="plan")
    done_when = _read_done_when(getattr(args, "done_when_json", "") or "")
    affected_paths = [str(v).strip() for v in (args.affected_path or []) if str(v or "").strip()]
    attach = [str(v).strip() for v in (args.attach or []) if str(v or "").strip()]
    ask_raw = [str(v).strip() for v in (args.ask or []) if str(v or "").strip()]
    ask = "council" if not ask_raw or ask_raw == ["council"] else ask_raw
    ctx, drive_root, subject_root, task_id = _context(args)
    slots = [
        {"slot_id": s.slot_id, "model": s.model, "route": getattr(s.route, "value", str(s.route)), "effort": s.effort}
        for s in plan_ask.plan_review_slots()
    ]
    sep = "=" * 80
    config = {
        "slots": slots, "drive_root": str(drive_root), "system_repo_root": str(REPO), "subject_root": str(subject_root),
        "plan": str(pathlib.Path(args.plan).expanduser().resolve(strict=False)),
        "done_when": done_when, "affected_paths": affected_paths, "attach": attach, "ask": ask, "task_id": task_id,
    }
    if getattr(args, "dry_run", False):
        # Assemble exactly what an ask would send, then stop. The operator entry point runs
        # OUTSIDE a task, where the budget gate has no usage scope and therefore no cost
        # ceiling — so the cheap way to see the bill first is to build and measure.
        state_root, _ = plan_ask.state_location(ctx)
        system_root, active_root = plan_ask.planning_roots(ctx)
        fields = plan_record.compose_revision(None, goal=str(args.goal or ""), plan=plan, done_when=done_when,
                                              affected_paths=affected_paths)
        body, body_note = plan_ask.plan_is_body(ctx, fields["affected_paths"], system_root=system_root, active_root=active_root)
        revision = dict(fields, n=1, body=body, body_note=body_note, ts="")
        prepared = plan_ask.prepare_ask(
            ctx, record=plan_record.empty_record(), revision=revision, ask=ask, message=str(args.message or ""),
            attach=attach, reviewer_effort="", state_root=state_root, task_id=task_id)
        return "\n".join([
            sep, "PLAN ASK DRY RUN (deliveries assembled; NOTHING dispatched, nothing written)", sep,
            json.dumps({
                **config, "body": body, "body_note": body_note,
                "participants": [{"seat_id": p.seat_id, "model": str(p.slot.model), "owner_marked": p.owner_marked,
                                  "host_added": p.host_added} for p in prepared.participants],
                "not_sendable": prepared.not_sendable,
                "system_prompt_chars": len(prepared.system_prompt),
                "content_chars_per_participant": {sid: len(text) for sid, text in prepared.contents.items()},
                "approx_tokens_per_participant": {sid: (len(prepared.system_prompt) + len(text)) // 4
                                                  for sid, text in prepared.contents.items()},
                "attached": [{k: v for k, v in item.items() if k != "text"} for item in prepared.manifest.get("attached") or []],
                "omissions": prepared.manifest.get("omissions") or [],
            }, ensure_ascii=False, indent=2, default=str),
            sep, "FIRST PARTICIPANT'S DELIVERY (system prompt, then content)", sep,
            prepared.system_prompt, "", next(iter(prepared.contents.values()), ""),
        ])
    write_task_result(drive_root, task_id, "running", root_task_id=task_id, task_attempt=1)
    text = plan_review._handle_plan_task(
        ctx, goal=str(args.goal or ""), plan=plan, done_when=done_when, affected_paths=affected_paths,
        ask=ask, message=str(args.message or ""), attach=attach)
    record = plan_record.load_plan_record(drive_root, task_id)
    return "\n".join([
        sep, "RESOLVED PLAN CONFIG", sep, json.dumps(config, ensure_ascii=False, indent=2, default=str),
        sep, "PLAN RECORD (the revision, every participant's typed state and reply)", sep,
        json.dumps(record, ensure_ascii=False, indent=2, default=str),
        sep, "PLAN TASK OUTPUT", sep, text,
    ])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write one plan revision and ask the configured council over it (the planning organ plan_task uses).",
    )
    parser.add_argument("--goal", default="", help="The outcome the work serves.")
    parser.add_argument("--plan", required=True, help="Path to the plan prose file.")
    parser.add_argument("--done-when-json", default="", help="Path to a JSON array of {id, claim, support} outcomes.")
    parser.add_argument("--affected-path", action="append", default=[],
                        help="A file the work will change (relative to the subject root, absolute or file://); repeatable; none = [].")
    parser.add_argument("--attach", action="append", default=[],
                        help="Evidence locator for the participants (path, task:<id>, chat:<id>); repeatable.")
    parser.add_argument("--ask", action="append", default=[],
                        help="Participant id to ask; repeatable. Omitted or 'council' = the review pool.")
    parser.add_argument("--message", default="", help="What you want from the participants now.")
    parser.add_argument("--subject-root", default="",
                        help="Active repository/workspace the plan is about; governance remains the Ouroboros repository.")
    parser.add_argument("--drive-root", default=os.environ.get("OUROBOROS_REVIEW_DRIVE_ROOT", ""),
                        help="Drive root for plan record/observability writes. Prefer a temp dir.")
    parser.add_argument("--task-id", default="", help="Task id for this run (default plan-review-cli); distinct ids keep separate records.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Assemble every participant's delivery and print its size, then stop BEFORE any paid dispatch.")
    parser.add_argument("--output", default="", help="Optional path to also write the full output.")
    args = parser.parse_args()

    _load_settings_into_env()
    output = _run(args)
    print(output)
    if args.output:
        pathlib.Path(args.output).expanduser().write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
