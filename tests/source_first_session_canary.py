"""Opt-in #1329 real consumer canary; default is preparation only, with no engine I/O.

Run with the candidate on sys.path, an existing authorized engine config directory,
and an explicitly selected subscription route. This script never provisions an
engine or edits its settings. It only substitutes gateway construction to connect
all existing review/custody callers to that same already-running engine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import secrets
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch


def summary_observation(actor: dict) -> dict:
    """Read the canary's JSON observation, never salvage a product verdict.

    The goal asks the summary to contain JSON, not to contain *only* JSON.
    The production parser must already have accepted the outer verdict.
    """
    parsed = actor.get("parsed")
    if actor.get("parse_status") != "valid" or not isinstance(parsed, dict):
        return {}
    summary = parsed.get("summary")
    if not isinstance(summary, str):
        return {}
    try:
        observed, _end = json.JSONDecoder().raw_decode(summary.lstrip())
    except ValueError:
        return {}
    return observed if isinstance(observed, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Spend one authorized subscription review session")
    parser.add_argument("--engine-config-dir", type=Path, help="Existing CLAUDEXOR_CONFIG_DIR; never copied or logged")
    parser.add_argument("--route", required=True, help="Exact existing harness=model target, without effort suffix")
    parser.add_argument("--profile", default="", help="Existing credential profile id; empty means engine selection")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--workspace-kind", choices=("plain", "git"), default="plain")
    parser.add_argument("--out", required=True, type=Path, help="New retained evidence directory, outside the test workspace")
    args = parser.parse_args()
    if args.run and not args.engine_config_dir:
        parser.error("--run requires the host's existing --engine-config-dir")
    if args.max_seconds <= 0:
        parser.error("--max-seconds must be positive")
    root = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    # Preserve neither caller settings nor provider/API credentials in the test process.
    environment = runpy.run_path(str(root / "ouroboros/test_environment.py"))["isolated_environment"]
    env = environment(out / "environment", root)
    os.environ.clear()
    os.environ.update(env)
    sys.path.insert(0, str(root))
    from ouroboros.acceptance_retrieving import acceptance_retrieving_work_order, retain_review_source
    from ouroboros.review_execution import ReviewRouteKind, session_route_for_review_slot
    from ouroboros.review_records import ReviewRequest, ReviewSlot
    from ouroboros.review_substrate import ReviewCoordinator
    from ouroboros.review_source_closure import retain_review_request_sources
    from ouroboros.artifacts import store_task_artifact_bytes, read_actor_source_bytes
    from ouroboros.outcome_receipt_store import append_verification_receipt
    from ouroboros.task_results import write_task_result
    from ouroboros.utils import append_jsonl
    from ouroboros.utils import sanitize_tool_result_for_log

    def save(name, body):
        (out / name).write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    workspace, author, canonical = out / "workspace", out / "author-drive", out / "canonical"
    workspace.mkdir()
    if args.workspace_kind == "git":
        subprocess.run(["git", "init", "-q", str(workspace)], check=True, env=env)
    workspace_marker = secrets.token_hex(24)
    (workspace / "workspace-marker.txt").write_text(workspace_marker, encoding="utf-8")
    probes = [secrets.token_hex(24) for _ in range(9001)]
    expected = {"begin": probes[0], "middle": probes[4500], "end": probes[-1], "workspace": workspace_marker}
    expected.update({key: secrets.token_hex(24) for key in ("task_result", "artifact", "receipt", "trajectory")})
    blind_markers = tuple(expected.values())
    request = ReviewRequest(
        surface="task_acceptance", task_id="source-canary-" + secrets.token_hex(8),
        goal=("Qualify this source delivery using your ordinary read-only file tools. Read the complete packet "
              "and workspace-marker.txt in your actual task workspace. The summary string of your acceptance "
              "JSON MUST itself contain a JSON object with keys begin, middle, end (values of evidence.probes "
              "at indices 0, 4500, 9000), workspace (exact workspace-marker.txt content), source_sha256 and "
              "source_bytes (compute from the complete packet file), scope_root (your actual working root). "
              "Also read the named task-result, artifact-inventory and its canary-source.txt, verification-receipts, "
              "and tool-trajectory sources. Report task_result (result field), artifact (file content), "
              "receipt (check field), trajectory (result field) in the same summary JSON. "
              "Do not substitute the source directory for your workspace. If any source cannot be read, "
              "return DEGRADED with the concrete gap. Read only these named sources and workspace marker."),
        scope="Read-only source delivery qualification; no code review, changes or external requests.",
        checklist="Report all eight blind markers and complete source byte identity in the summary JSON.",
        subject="Synthetic canary; no product acceptance or review approval is sought.",
        evidence={"probes": probes}, policy={"min_successful_slots": 1})
    slot = ReviewSlot(slot_id="session-source-canary", model=args.route, route=ReviewRouteKind.AGENT_SESSION,
                      session_target=args.route, session_profile=args.profile, effort=args.effort,
                      timeout_sec=args.max_seconds)
    # Actual host-owned sources, not fabricated closure metadata. The closure
    # owner snapshots these and names their retained reader locations.
    write_task_result(author, request.task_id, "completed", result=expected["task_result"])
    store_task_artifact_bytes(author, request.task_id, "canary-source.txt", expected["artifact"].encode())
    assert append_verification_receipt(author, request.task_id, {
        "tool": "verify_and_record", "status": "pass", "check": expected["receipt"]})
    append_jsonl(author / "logs" / "tools.jsonl", {"task_id": request.task_id, "result": expected["trajectory"]})
    retain_review_request_sources(request, source_root=author, custody_root=canonical)
    acceptance_retrieving_work_order(request, [slot], session_root=str(workspace),
                                     data_root=Path(request.policy["native_data_root"]))
    delivery = request.slot_source_delivery[slot.slot_id]
    if delivery["status"] != "paged":
        save("preparation-failure.json", delivery)
        return 2
    save("prepared-request.json", {"request": asdict(request), "slot": asdict(slot)})
    def retained_named_sources():
        closure = request.policy["review_source_closure"]
        named = {row["name"]: row for row in closure["sources"] if row["status"] == "retained"}
        for name, key in (("task-result", "task_result"), ("artifact:canary-source.txt", "artifact"),
                          ("verification-receipts", "receipt"), ("tool-trajectory", "trajectory")):
            row = named[name]
            raw = read_actor_source_bytes(closure["read_root"], request.task_id, row["source_ref"])
            assert Path(row["retained_path"]).read_bytes() == raw and expected[key].encode() in raw
        inventory = json.loads(Path(named["artifact-inventory"]["retained_path"]).read_bytes())
        assert inventory["artifacts"][0]["retained_path"] == named["artifact:canary-source.txt"]["retained_path"]
        return closure

    if not args.run:
        retain_review_source(request, slot.slot_id, canonical)
        shutil.rmtree(author)
        save("named-source-proof.json", retained_named_sources())
        save("preparation.json", {"state": "PREPARED_ONLY", "actual_session_read": "NOT_RUN",
                                  "source_delivery": delivery, "slot": asdict(slot)})
        print(f"PREPARED_ONLY; no engine contacted. Evidence: {out}")
        return 0

    from ouroboros.gateways.claudexor import ClaudexorGateway, discover_daemon_at
    try:
        endpoint = discover_daemon_at(args.engine_config_dir)  # token stays only in the existing client, never saved
        route = session_route_for_review_slot(slot)
        with ClaudexorGateway(endpoint=endpoint) as gateway:
            hello = gateway.handshake()
            catalog = gateway.agent_capabilities()
            harnesses = gateway.harnesses()
            save("engine-facts.json", {"engine": hello.get("engine"), "protocolMajor": hello.get("protocolMajor"),
                 "selected_route": asdict(route), "runControlKeys": catalog.get("runControlKeys"),
                 "catalog": [{k: r.get(k) for k in ("id", "status", "accessProfilesSupported", "attachmentInputs")}
                             for r in catalog.get("harnesses", []) if r.get("id") == route.route_id],
                 "harness": [{"id": r.get("id"), "capabilities": (r.get("manifest") or {}).get("capabilities")}
                             for r in harnesses if r.get("id") == route.route_id]})
    except Exception as exc:
        save("engine-unavailable.json", {"state": "NOT_RUN", "type": type(exc).__name__,
                                        "error": sanitize_tool_result_for_log(str(exc))})
        print(f"NOT_RUN; inspect retained evidence: {out}")
        return 1

    class ObservedGateway(ClaudexorGateway):
        def __init__(self, *unused, **ignored):
            super().__init__(endpoint=endpoint)
            self.handshake()

        def start_run(self, wire, **kwargs):
            # Coordinator + executor constructed this exact wire request, not the script.
            delivery = request.slot_source_delivery[slot.slot_id]
            raw = Path(delivery["source_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == delivery["source"]["sha256"]
            assert Path(delivery["source_path"]).is_relative_to(canonical)
            assert json.dumps(delivery["source_path"], ensure_ascii=False) in wire["prompt"]
            assert wire["scope"] == {"kind": "project", "root": str(workspace)}
            assert wire["access"] == "readonly" and wire["mode"] == "ask"
            assert wire["authPreference"] == "subscription" and "attachments" not in wire
            rendered = json.dumps(wire, ensure_ascii=False)
            assert not any(marker in rendered for marker in blind_markers)
            assert len(rendered) == delivery["first_send_chars"] < delivery["first_send_ceiling"]
            expected.update(source_sha256=hashlib.sha256(raw).hexdigest(), source_bytes=len(raw), scope_root=str(workspace))
            save("first-send.json", wire)
            save("source-delivery.json", delivery)
            # Only disposable canary data is removed. Canonical custody survives before POST.
            if author.exists():
                shutil.rmtree(author)
            save("named-source-proof.json", retained_named_sources())
            handle = super().start_run(wire, **kwargs)
            save("run-handle.json", handle)
            return handle

    class NoAPI:
        def chat(self, **kwargs):
            raise RuntimeError("Canary forbids API/extraction fallback; preserve the actual session answer")

    try:
        # These two client factories are the ONLY substitutions; transport, route
        # health, coordinator, executor, custody, wait, parsing and settlement are real.
        with patch("ouroboros.gateways.claudexor.ClaudexorGateway", ObservedGateway), \
             patch("ouroboros.claudexor_daemon.ensure_owned_gateway", ObservedGateway):
            result = ReviewCoordinator(llm=NoAPI(), drive_root=canonical).run(request, [slot])
        delivery = request.slot_source_delivery[slot.slot_id]
        save("consumer-result.json", asdict(result))
        actor = result.actors[0]
        parsed = actor.get("parsed")
        observed = summary_observation(actor)
        matches = {k: observed.get(k) == value for k, value in expected.items()}
        qualified = bool(actor.get("status") == "ok" and isinstance(parsed, dict)
                         and actor.get("parse_status") == "valid" and parsed.get("verdict") == "PASS"
                         and len(matches) == 11 and all(matches.values()) and not author.exists())
        save("read-proof.json", {"state": "QUALIFIED" if qualified else "NOT_QUALIFIED", "matches": matches,
             "expected": expected, "observed": observed, "source_delivery": delivery,
             "actual_route_profile_facts": actor.get("usage"),
             "limitations": "Blind sample/whole-file identity proof on this selected route only; not proof of comprehension. "
                            "Read diagnostics do not gate product verdict/quorum. Custody lifetime remains with its owner."})
        print(f"{'QUALIFIED' if qualified else 'NOT_QUALIFIED'}; evidence: {out}")
        return 0 if qualified else 1
    except Exception as exc:
        save("canary-error.json", {"state": "NOT_QUALIFIED", "error": sanitize_tool_result_for_log(str(exc)),
                                   "type": type(exc).__name__, "source_delivery": delivery})
        print(f"NOT_QUALIFIED; inspect retained evidence: {out}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
