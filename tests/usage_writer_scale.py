"""Opt-in SERIAL spawned-process qualification with synthetic data and local sends.

Run through scripts/safe_test.py: python -m tests.usage_writer_scale --seconds 60
--writers 10 24. Outputs live under the launcher's retained temporary root.
No provider, production data, timeout override or compaction bypass is used.
The fixture has real transition chains and candidate/processing provenance;
--attempts controls semantic scale, not padding. This is also usable on a base
checkout for separately labelled baseline measurements.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import multiprocessing as mp
import os
import pathlib
import platform
import shutil
import statistics
import subprocess
import sys
import time
import traceback
from decimal import Decimal, ROUND_HALF_EVEN, localcontext


def fixture(root, attempts=30000):
    from ouroboros import usage_accounting as ua
    from ouroboros import usage_compaction as compaction
    from ouroboros.utils import utc_now_iso

    root.mkdir(parents=True, exist_ok=True)
    ua.ensure_legacy_imported(root)
    now = utc_now_iso()
    rows = 0
    with open(root / ua.LEDGER_REL, "wb") as handle:
        for index in range(attempts):
            identity = f"synthetic-{index:08d}"
            digest = hashlib.sha256(identity.encode()).hexdigest()
            base = {
                "attempt_id": identity, "kind": "attempt", "provider": "local", "model": "local-stub",
                "task_id": f"task-{index % 64}", "root_task_id": "dominant" if index % 10 < 7 else f"root-{index % 16}",
                "parent_task_id": "parent", "category": "task", "source": "scale_fixture",
                "reservation_upper_bound_usd": .012345, "pricing_known": True,
                "reservation_basis": "explicit_upper_bound", "root_limit_usd": 1000000,
                "global_limit_usd": 1000000, "global_limit_source": "attempt_request",
                "candidate_measurement_kind": "canonical_json_v1", "candidate_raw_sha256": digest,
                "candidate_context_sha256": hashlib.sha256((identity + "context").encode()).hexdigest(),
                "candidate_raw_size_bytes": 18347 + index, "candidate_context_size_bytes": 16180 + index,
                "candidate_manifest_ref": {"call_id": identity, "sha256": digest},
                "physical_context": {"profile": "owner_max", "rendered_mode": "max",
                    "measurement_basis": "fresh_route_usage", "target_total_tokens": 32000,
                    "capacity_total_tokens": 128000, "context_target_miss": False,
                    "automatic_pass_used": False, "route_fp": "local:synthetic", "round_id": str(index)},
                "processing_preference": "standard", "submitted_processing_mode": "default",
                "prompt_tokens": 4096 + index % 256, "completion_tokens": 1024 + index % 128,
                "cached_tokens": 2048, "cache_write_tokens": 1024,
                "processing": {"observed": "default", "requested": "standard"},
                "cost_evidence": {"knowledge": "exact", "cashUsd": .000031,
                                  "valuationKnowledge": "unknown", "valuationUsd": None},
                # Most resolved chains are young and legitimately survive compaction.
                "ts": "2020-01-01T00:00:00Z" if index % 10 == 0 else now,
            }
            states = ["reserved", "dispatched"]
            if index % 19 == 0:
                states += ["unresolved", "settled"]
            elif index % 11 == 0:
                states += ["unresolved"]
            else:
                states += ["settled"]
            for state in states:
                row = {**base, "state": state, "seq": rows + 1}
                if state == "settled":
                    if index % 19 == 0:
                        row.update(cost_usd=None, cost_final=False, settle_reason="abandoned")
                    else:
                        row.update(cost_usd=None if index % 31 == 0 else .000031,
                                   cost_final=index % 31 != 0 and index % 23 != 0)
                handle.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
                rows += 1
        for index in range(max(1, attempts // 27)):
            row = {"seq": rows + 1, "attempt_id": f"session-{index}", "kind": "subscription_session",
                   "state": "settled", "root_task_id": "dominant", "task_id": f"task-{index % 64}",
                   "provider": "subscription", "model": "observed-model", "subscription_route": "local-subscription",
                   "subscription_reset_at": "2030-01-01T00:00:00Z", "cost_usd": None if index % 2 else 0,
                   "cost_final": False, "reservation_upper_bound_usd": None, "ts": now}
            handle.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
            rows += 1
        handle.flush()
        os.fsync(handle.fileno())
    original = {"rows": rows, "bytes": (root / ua.LEDGER_REL).stat().st_size}
    started = time.monotonic()
    with ua._locked(root) as beat:
        receipt = compaction.compact_usage_ledger_locked(root, heartbeat=beat)
    if receipt is None:
        raise AssertionError("fixture's initial real compaction did not commit")
    return {**original, "compaction_sec": time.monotonic() - started, "compaction": receipt,
            "active_sha256": hashlib.sha256((root / ua.LEDGER_REL).read_bytes()).hexdigest(),
            "active_bytes": (root / ua.LEDGER_REL).stat().st_size,
            "active_rows": sum(1 for _ in open(root / ua.LEDGER_REL, "rb"))}


def raw_cash(root):
    """Quiescent independent oracle, precision 200, raw Decimal literals."""
    from ouroboros.usage_ledger import LEDGER_REL

    finals = {}
    for line in open(root / LEDGER_REL, "rb"):
        row = json.loads(line, parse_float=Decimal)
        finals[row["attempt_id"]] = row
    totals = [Decimal(0)] * 5
    with localcontext() as context:
        context.prec = 200
        for row in finals.values():
            if row.get("kind") in {"usage_baseline", "legacy_metadata"}:
                continue
            cost, bound = row.get("cost_usd"), row.get("reservation_upper_bound_usd")
            if row["state"] == "settled" and cost is not None:
                value = Decimal(str(cost))
                totals[0] += value
                totals[1 if row.get("cost_final") else 2] += value
            elif row["state"] in {"reserved", "dispatched", "unresolved", "settled"} and bound is not None:
                totals[3 if row["state"] == "reserved" else 4] += Decimal(str(bound))
    return totals, finals


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {"count": len(ordered), "p50": statistics.median(ordered),
            "p95": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
            "p99": ordered[min(len(ordered) - 1, int(len(ordered) * .99))], "max": max(ordered)}


def owned_queue(root, writers):
    """Real queue shape: running shared-root writers and a fenced pending sibling."""
    from ouroboros.utils import atomic_write_json, utc_now_iso

    fence_id = "scale-root-fence"
    def member(index):
        return {"task": {
            "id": f"writer-{index}", "root_task_id": "dominant", "chat_id": 1,
            "_attempt": 1, "type": "task", "status": "running",
            "objective": "Verify the isolated ledger through reserve, dispatch and settlement.",
            "metadata": {"root_task_id": "dominant", "parent_task_id": "scale-root",
                         "delegation_role": "subagent", "root_limit_usd": 1000000},
            "task_contract": {"objective": "Account one local stub receipt per physical attempt.",
                              "expected_output": "Measured progress and exact monetary facts.",
                              "constraints": "Synthetic data; local receipt; no external provider."},
        }, "started_at": time.time(), "last_activity_at": time.time(), "worker_id": index}
    pending = [member(i) for i in range(writers, writers + 32)]
    for row in pending:
        row["task"].update(status="pending", root_task_id="paused-sibling",
                           _budget_pause_hold={"fence_id": fence_id, "selected": False})
        row["task"]["metadata"]["root_task_id"] = "paused-sibling"
    snapshot = {"running": [member(i) for i in range(writers)], "pending": pending,
                "budget_root_fences": [{"root_task_id": "paused-sibling", "status": "active",
                                        "fence_id": fence_id}], "updated_at": utc_now_iso()}
    path = root / "state" / "queue_snapshot.json"
    atomic_write_json(path, snapshot)
    return {"bytes": path.stat().st_size, "running": writers, "pending": 32,
            "fences": 1, "active_fence_root": "paused-sibling", "owner_control": "native readers"}


def _worker(root_text, index, phase, ready, start, seconds, output, worker_source=None, owned=False):
    stats = {"writer": index, "pid": os.getpid(), "stage": {}, "wait": {}, "hold": {},
             "preparation": [], "parsed_rows": 0, "full_reads": 0, "cycles": 0, "longest_gap": 0.0,
             "strict_reads": 0, "wait_episodes": 0, "episode_elapsed": [], "continuity_extended_waits": 0,
             "ready": False, "owned": owned}
    try:
        _run_writer(stats, root_text, index, phase, ready, start, seconds, output, worker_source, owned)
    except BaseException:
        stats["error"] = traceback.format_exc()
    finally:
        stats.pop("_last_acquisition_began", None)
        previous = stats.pop("previous", None)
        if previous is not None:
            stats["longest_gap"] = max(stats["longest_gap"], time.monotonic() - previous)
        if sys.platform != "win32":
            import resource  # guarded native measurement, never a runtime dependency
            stats["rss_peak_native"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            stats["rss_unit"] = "bytes" if sys.platform == "darwin" else "KiB"
        else:
            stats["rss_peak_native"] = None
        (output / f"writer-{index}.json").write_text(json.dumps(stats, indent=2))
        if not stats["ready"]:
            ready.put({"actor": str(index), "ready": False, "error": stats.get("error")})
    if stats.get("error"):
        raise SystemExit(1)


def _run_writer(stats, root_text, index, phase, ready, start, seconds, output, worker_source, owned):
    if worker_source:
        sys.path.insert(0, worker_source)
    from ouroboros import usage_accounting as ua
    from ouroboros import _usage_rows_memo as memo

    root = pathlib.Path(root_text)
    import importlib
    try:
        wait_module = importlib.import_module("ouroboros._usage_wait")
    except ModuleNotFoundError as exc:
        if exc.name != "ouroboros._usage_wait":
            raise
        wait_module = None
    stats["imported_source"] = ua.__file__
    stats["wait_instrumentation"] = "available" if wait_module else "baseline_module_absent"
    if wait_module is not None:
        original_hold = wait_module._hold
        def observed_hold(owner, phase, started, *identity):
            if phase == "entered":
                stats["wait_episodes"] += 1
                stats["_episode_acquired"] = None
                stats["_episode_started"] = stats["_last_acquisition_began"]
            if phase == "ended":
                acquired = stats.pop("_episode_acquired", None)
                elapsed = (acquired if acquired is not None else time.monotonic()) - stats.pop("_episode_started")
                stats["episode_elapsed"].append(elapsed)
                stats["continuity_extended_waits"] += int(elapsed > 45)
            return original_hold(owner, phase, started, *identity)
        wait_module._hold = observed_hold
    stage = "warmup"
    real_lock = ua._locked
    real_read = ua._read_records_locked
    real_delta = ua._read_new_records_locked
    in_lock = False

    @contextlib.contextmanager
    def measured_lock(*args, **kwargs):
        nonlocal in_lock
        began = stats["_last_acquisition_began"] = time.monotonic()
        acquired = None
        try:
            with real_lock(*args, **kwargs) as beat:
                acquired = time.monotonic()
                if "_episode_acquired" in stats:
                    stats["_episode_acquired"] = acquired
                stats["wait"].setdefault(stage, []).append(acquired - began)
                in_lock = True
                try:
                    yield beat
                finally:
                    in_lock = False
                    stats["hold"].setdefault(stage, []).append(time.monotonic() - acquired)
        finally:
            if acquired is None:
                stats["wait"].setdefault(stage, []).append(time.monotonic() - began)

    def read(*args, **kwargs):
        result = real_read(*args, **kwargs)
        stats["full_reads"] += 1
        stats["parsed_rows"] += len(result)
        return result

    def delta(*args, **kwargs):
        result = real_delta(*args, **kwargs)
        if result is not None:
            stats["parsed_rows"] += len(result[0])
        return result

    ua._locked, ua._read_records_locked, ua._read_new_records_locked = measured_lock, read, delta
    if owned:
        from ouroboros.model_wait import TaskModelWait
        def observe(function, kind):
            def call(*args, **kwargs):
                began = time.monotonic()
                try:
                    return function(*args, **kwargs)
                finally:
                    stats.setdefault(kind, {}).setdefault("in_lock" if in_lock else "outside_lock", []).append(time.monotonic() - began)
            return call
        if hasattr(ua, "_check_dispatch_fences"):
            ua._check_dispatch_fences = observe(ua._check_dispatch_fences, "fence_checks")
        else:
            stats["fence_instrumentation"] = "baseline_helper_absent"

        TaskModelWait.control_reason = observe(TaskModelWait.control_reason, "control_checks")
    if hasattr(memo, "_prepare_writer"):
        real_prepare = memo._prepare_writer

        def prepare(*args):
            began = time.monotonic()
            result = real_prepare(*args)
            stats["preparation"].append(time.monotonic() - began)
            if isinstance(result, memo._LedgerWriterView):
                stats["parsed_rows"] += len(result.records)
            return result
        memo._prepare_writer = prepare
    if phase != "cold":
        if hasattr(memo, "_writer_locked"):
            with memo._writer_locked(root):
                pass
        else:
            with ua._locked(root):
                ua._read_records_locked_cached(root)
    stats["warmup_wait"] = stats["wait"]
    stats["warmup_hold"] = stats["hold"]
    stats["wait"], stats["hold"] = {}, {}
    ready.put({"actor": str(index), "ready": True})
    stats["ready"] = True
    if not start.wait(180):
        raise TimeoutError("phase start not received")
    began = previous = time.monotonic()
    stats["previous"] = previous
    sends = open(output / f"sends-{index}.jsonl", "a", buffering=1)
    task = (json.loads((root / "state" / "queue_snapshot.json").read_text())["running"][index]["task"]
            if owned else None)
    if owned:
        from ouroboros.model_wait import task_model_wait_scope
    scope = (task_model_wait_scope(task=task, drive_root=root, event_queue=None, worker_slot_held=True)
             if owned else contextlib.nullcontext())
    try:
        with scope:
            while time.monotonic() - began < seconds:
                held = None
                for stage in ("strict_read", "reserve", "dispatch", "send", "settle"):
                    entered = time.monotonic()
                    if stage == "strict_read":
                        ua.usage_projection(root, root_task_id="dominant")
                        stats["strict_reads"] += 1
                    elif stage == "reserve":
                        held = ua.reserve_attempt(ua.AttemptRequest(model="local-stub", provider="local",
                            reservation_usd=.001, global_limit_usd=1000000, drive_root=root,
                            task_id=f"writer-{index}", root_task_id="dominant", root_limit_usd=1000000, source="scale_stub"))
                    elif stage == "dispatch":
                        ua.mark_dispatched(held)
                    elif stage == "send":
                        sends.write(json.dumps({"attempt_id": held.attempt_id, "writer": index}) + "\n")
                        sends.flush()
                        os.fsync(sends.fileno())
                    else:
                        ua.settle_attempt(held, {"prompt_tokens": 40, "completion_tokens": 12},
                                          cost_usd=.000031, cost_final=True)
                    stats["stage"].setdefault(stage, []).append(time.monotonic() - entered)
                now = time.monotonic()
                stats["longest_gap"] = max(stats["longest_gap"], now - previous)
                stats["cycles"] += 1
                previous = now
                stats["previous"] = previous
    finally:
        sends.close()
    stats["elapsed_sec"] = time.monotonic() - began
    stats["strict_reads_per_sec"] = stats["strict_reads"] / stats["elapsed_sec"]


def _audit_holder(root_text, ready, start, seconds, output, worker_source=None):
    stats = {"cycles": 0, "wait": [], "hold": [], "full_records": [], "cadence_sec": 5, "ready": False}
    try:
        if worker_source:
            sys.path.insert(0, worker_source)
        from ouroboros import usage_accounting as ua
        from ouroboros.model_send_seal import reconcile_model_send_seals
        from ouroboros.server_maintenance import _reconcile_abandoned_usage
        root = pathlib.Path(root_text)
        stats["imported_source"] = ua.__file__
        real_lock = ua._locked
        @contextlib.contextmanager
        def measured(*args, **kwargs):
            began = time.monotonic()
            with real_lock(*args, **kwargs) as beat:
                acquired = time.monotonic()
                stats["wait"].append(acquired - began)
                try:
                    yield beat
                finally:
                    stats["hold"].append(time.monotonic() - acquired)
        ua._locked = measured
        ready.put({"actor": "audit", "ready": True})
        stats["ready"] = True
        if not start.wait(180):
            raise TimeoutError("audit start not received")
        began = time.monotonic()
        while True:
            tick = time.monotonic()
            # Full-record snapshot + validation audit, not a sleep pretending to
            # hold a lock. Legacy baselines without the helper remain labelled.
            if hasattr(ua, "read_usage_records"):
                records = ua.read_usage_records(root)
            else:
                with ua._locked(root):
                    records = ua._read_records_locked(root)
            ua._validate_records(records)
            stats["full_records"].append(len(records))
            del records
            report = reconcile_model_send_seals(root)
            if report["status"] != "completed":
                raise AssertionError(f"seal audit incomplete: {report}")
            _reconcile_abandoned_usage(root)
            stats["cycles"] += 1
            if time.monotonic() - began >= seconds:
                break
            time.sleep(max(0, min(5 - (time.monotonic() - tick), seconds - (time.monotonic() - began))))
        stats["elapsed_sec"] = time.monotonic() - began
    except BaseException:
        stats["error"] = traceback.format_exc()
    finally:
        (output / "audit.json").write_text(json.dumps(stats, indent=2))
        if not stats["ready"]:
            ready.put({"actor": "audit", "ready": False, "error": stats.get("error")})
    if stats.get("error"):
        raise SystemExit(1)


def raw_summary(rows):
    """Independent summary oracle over raw JSON, not product fold helpers."""
    totals = [Decimal(0)] * 5
    counts, windows, processing = {}, {}, {}
    unknown = priced = tracked = opened = nonfinal = sessions = 0
    with localcontext() as context:
        context.prec = 200
        for row in rows:
            kind, state = row.get("kind"), row["state"]
            if kind == "usage_baseline":
                continue
            if kind == "legacy_metadata":
                counts["metadata_only"] = counts.get("metadata_only", 0) + max(1, int(row.get("ambiguous_call_count") or 1))
                continue
            weight = max(1, int(row.get("folded_attempt_count") or 1)) if kind == "usage_baseline_group" else 1
            counts[state] = counts.get(state, 0) + weight
            cost, bound = row.get("cost_usd"), row.get("reservation_upper_bound_usd")
            if state == "settled" and cost is not None:
                totals[0] += Decimal(str(cost))
                totals[1 if row.get("cost_final") else 2] += Decimal(str(cost))
                priced += weight
                if not row.get("cost_final"):
                    nonfinal += weight
                    tracked += weight
                    opened += weight
            elif state in {"settled", "reserved", "dispatched", "unresolved"}:
                nonfinal += weight
                unknown += weight if state == "settled" or bound is None or row.get("pricing_known") is False else 0
                if bound is not None:
                    totals[3 if state == "reserved" else 4] += Decimal(str(bound))
                    priced += weight
                    tracked += weight
                if state != "settled" or bound is not None:
                    opened += weight
            if kind == "subscription_session":
                sessions += 1
                route, reset = row.get("subscription_route"), row.get("subscription_reset_at")
                if route and reset:
                    windows[route] = max(windows.get(route, ""), reset)
            facts = row.get("processing_summary")
            if facts is None:
                evidence = row.get("cost_evidence") or {}
                entries = row.get("attempt_execution")
                if not isinstance(entries, list):
                    entries = [{"processing": row.get("processing"),
                                "processingCostBasis": evidence.get("processing") or row.get("processing_basis"),
                                "usageCost": {"valuationUsd": evidence.get("valuationUsd"),
                                              "valuationKnowledge": evidence.get("valuationKnowledge", "unknown"),
                                              "cashUsd": evidence.get("cashUsd") if evidence.get("cashUsd") is not None else evidence.get("estimatedUsd"),
                                              "cashKnowledge": evidence.get("knowledge", "unknown")} if evidence else {}}]
                facts = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    receipt, basis, usage = entry.get("processing") or {}, entry.get("processingCostBasis") or {}, entry.get("usageCost") or {}
                    for key, label in (("observed_modes", receipt.get("observed")), ("billing_kinds", basis.get("kind"))):
                        if label:
                            bucket = facts.setdefault(key, {})
                            bucket[label] = bucket.get(label, 0) + 1
                    if usage:
                        known = usage.get("valuationKnowledge") in {"exact", "estimated"}
                        for key, value in (("unknown_cash_rows", usage.get("cashKnowledge") not in {"exact", "estimated"} or usage.get("cashUsd") is None),
                                           ("unknown_valuation_rows", not known or usage.get("valuationUsd") is None)):
                            facts[key] = facts.get(key, 0) + int(value)
                        for key, value in (("valuation_usd", usage.get("valuationUsd") if known else None), ("unclassified_usd", usage.get("unknownUsd"))):
                            if value is not None:
                                facts[key] = facts.get(key, Decimal(0)) + Decimal(str(value))
            for key, value in facts.items():
                if isinstance(value, dict):
                    bucket = processing.setdefault(key, {})
                    for label, count in value.items():
                        bucket[label] = bucket.get(label, 0) + count
                elif value is not None:
                    processing[key] = processing.get(key, 0) + (Decimal(str(value)) if key.endswith("_usd") else value)
        rounded = [x.quantize(Decimal(".000001"), rounding=ROUND_HALF_EVEN) for x in totals]
        result = dict(zip(("settled_usd", "confirmed_usd", "estimated_usd", "reserved_usd", "unresolved_upper_bound_usd"), map(float, rounded)))
        result["accounted_usd"] = float(rounded[0] + rounded[3] + rounded[4])
    result.update(unknown_unmetered=unknown, priced_rows=priced, tracked_nonfinal_rows=tracked,
                  accounting_open_rows=opened, non_final_rows=nonfinal, cost_final=not nonfinal,
                  attempt_counts=counts, subscription_sessions=sessions, subscription_windows=windows)
    if processing:
        result["processing_summary"] = {**processing, **{key: float(processing[key]) if key in processing else None
                                                       for key in ("valuation_usd", "unclassified_usd")}}
    return result


def raw_breakdown(rows):
    result = raw_summary(rows)
    physical, ttls = 0, {}
    for row in rows:
        kind, state = row.get("kind", "attempt"), row["state"]
        calls = 0
        if kind == "usage_baseline_group":
            calls = max(1, int(row.get("folded_attempt_count") or 1)) if state in {"settled", "unresolved"} else 0
        elif kind not in {"usage_baseline", "legacy_metadata", "legacy_delta", "subscription_session"}:
            calls = int(state in {"dispatched", "settled", "unresolved"})
        physical += calls
        ttl = str(row.get("prompt_cache_ttl") or "").strip()
        if ttl:
            ttls[ttl] = ttls.get(ttl, 0) + calls
    result.update(physical_calls=physical, prompt_cache_ttls=ttls)
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"):
        values = [max(0, int(row[key])) for row in rows if row.get(key) is not None]
        result[key] = sum(values) if values else None
    prompt, completion = result["prompt_tokens"], result["completion_tokens"]
    result["total_tokens"] = None if prompt is None and completion is None else (prompt or 0) + (completion or 0)
    return result


def phase_run(root, output, writers, phase, seconds, worker_source=None, owned=False):
    from ouroboros import usage_accounting as ua
    from ouroboros import usage_compaction as compaction

    output.mkdir(parents=True)
    context = mp.get_context("spawn")
    ready, start = context.Queue(), context.Event()
    processes = [context.Process(target=_worker, args=(str(root), index, phase, ready, start, seconds, output, worker_source, owned))
                 for index in range(writers)]
    audit = context.Process(target=_audit_holder, args=(str(root), ready, start, seconds, output, worker_source))
    processes.append(audit)
    receipt = None
    try:
        for process in processes:
            process.start()
        arrivals = []
        for _ in processes:
            receipt = ready.get(timeout=180)
            if not receipt["ready"]:
                raise RuntimeError(f"phase initialization failed: {receipt}")
            arrivals.append(receipt["actor"])
        assert len(set(arrivals)) == writers + 1
        receipt = None
        started = time.monotonic()
        start.set()
        if phase == "compaction":
            time.sleep(min(5, seconds / 4))
            entered = time.monotonic()
            with ua._locked(root) as beat:
                acquired = time.monotonic()
                # Introduce eligible history inside this same hold, so ordinary
                # opportunistic compaction cannot fold the test's replacement
                # witness before the measured explicit pass gets it.
                records = ua._read_records_locked_cached(root)
                additions = []
                for index in range(1000):
                    base = {"attempt_id": f"fold-during-phase-{index}", "kind": "attempt", "provider": "local",
                            "model": "local-stub", "root_task_id": "dominant", "reservation_upper_bound_usd": .01,
                            "ts": "2020-01-01T00:00:00Z"}
                    additions.extend({**base, "state": state, **(
                        {"cost_usd": .001, "cost_final": True} if state == "settled" else {})}
                        for state in ("reserved", "dispatched", "settled"))
                ua._append_rows_locked(root, records, additions)
                compact_started = time.monotonic()
                receipt = compaction.compact_usage_ledger_locked(root, heartbeat=beat)
                compaction_timing = {"wait": acquired - entered, "hold": time.monotonic() - compact_started,
                                     "fixture_append_hold": compact_started - acquired, "total_hold": time.monotonic() - acquired}
            if receipt is None:
                raise AssertionError("compaction phase must commit an actual replacement")
        for process in processes:
            process.join(max(1, started + seconds + 100 - time.monotonic()))
            if process.is_alive():
                raise TimeoutError(f"writer {process.pid} exceeded bounded phase custody")
    finally:
        # Only this run's owned children are reaped; no process census or signals
        # against another task, provider, installation or ledger.
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(10)
        ready.close()
        ready.join_thread()
    reports = [json.loads((output / f"writer-{i}.json").read_text()) for i in range(writers)]
    sends = [json.loads(line)["attempt_id"] for path in output.glob("sends-*.jsonl") for line in path.read_text().splitlines()]
    exact, finals = raw_cash(root)
    audit_report = json.loads((output / "audit.json").read_text())
    errors = [item["error"] for item in reports if item.get("error")]
    if audit_report.get("error") or not audit_report["cycles"]:
        errors.append(audit_report.get("error") or "audit holder made no progress")
    if len(sends) != len(set(sends)):
        errors.append("duplicate provider sends")
    if any(finals[identity]["state"] != "settled" for identity in sends):
        errors.append("sent ID is not settled")
    if hasattr(__import__("ouroboros._usage_rows_memo", fromlist=["_writer_locked"]), "_writer_locked"):
        from ouroboros._usage_rows_memo import _writer_locked
        with _writer_locked(root) as view:
            if tuple(exact) != view.cash:
                errors.append("independent raw-Decimal money mismatch")
            from ouroboros._usage_money import decimal_of
            def raw_values(value):
                if isinstance(value, float):
                    return decimal_of(value)
                if isinstance(value, dict):
                    return {key: raw_values(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [raw_values(item) for item in value]
                return value
            if raw_values(view.finals) != finals:
                errors.append("independent raw final-row money/nonmoney/metadata mismatch")
    projection = ua.usage_projection(root)
    expected = raw_summary(finals.values())
    if any(projection.get(key) != value for key, value in expected.items()):
        errors.append("independent raw money/nonmoney summary mismatch")
    for identity, bucket in projection["by_root"].items():
        expected = raw_summary([row for row in finals.values() if str(row.get("root_task_id") or "") == identity])
        if any(bucket.get(key) != value for key, value in expected.items()):
            errors.append(f"independent root money/nonmoney mismatch: {identity}")
    breakdown = ua.usage_breakdown(root)
    rows = list(finals.values())
    buckets = [("global", breakdown, rows), ("delegated", breakdown["delegated"],
                [row for row in rows if row.get("kind") == "subscription_session"])]
    for axis, field in (("model", "model"), ("provider", "provider"), ("category", "category"),
                        ("task", "task_id"), ("root", "root_task_id")):
        grouped, missing = {}, []
        for row in rows:
            identity = str(row.get(field) or "")
            if not identity or row.get("kind") in {"legacy_metadata", "legacy_delta"}:
                missing.append(row)
            else:
                grouped.setdefault(identity, []).append(row)
        if set(grouped) != set(breakdown["by_" + axis]):
            errors.append(f"independent breakdown {axis} identities mismatch")
        buckets.append((axis + ":unattributed", breakdown["unattributed"][axis], missing))
        buckets.extend((axis + ":" + key, breakdown["by_" + axis].get(key, {}), subset)
                       for key, subset in grouped.items())
    for label, actual, subset in buckets:
        if any(actual.get(key) != value for key, value in raw_breakdown(subset).items()):
            errors.append(f"independent money/nonmoney breakdown mismatch: {label}")
    for item in reports:
        item["raw_max_acquisition_wait"] = max([*item["episode_elapsed"], *(value for values in item["wait"].values() for value in values)], default=0)
        if item["raw_max_acquisition_wait"] >= 45:
            errors.append(f"writer {item['writer']} raw acquisition wait >=45s")
        if item["continuity_extended_waits"]:
            errors.append(f"writer {item['writer']} used continuity-extended wait")
    if any(item["cycles"] == 0 or item["longest_gap"] >= 45 for item in reports):
        errors.append("a writer made no progress or had a >=45s progress gap")
    if receipt and compaction_timing["total_hold"] >= 45:
        errors.append("one legal compaction held the monetary lock >=45s")
    result = {"phase": phase, "writers": writers, "seconds": seconds, "owned": owned,
              "throughput_cycles_sec": sum(item["cycles"] for item in reports) / max(item.get("elapsed_sec", seconds) for item in reports),
              "exitcodes": [process.exitcode for process in processes], "errors": errors,
              "audit": audit_report, "strict_read_cadence": "one strict root projection before every reserve/send cycle",
              "compaction": receipt, "compaction_timing": compaction_timing if receipt else None,
              "raw_decimal_cash": list(map(str, exact)), "send_count": len(sends),
              "per_writer": [{**{key: value for key, value in item.items() if key not in {"stage", "wait", "hold"}},
                              **{kind: {stage: distribution(values) for stage, values in item[kind].items()}
                                 for kind in ("stage", "wait", "hold")}} for item in reports]}
    (output / "result.json").write_text(json.dumps(result, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--writers", type=int, nargs="+", default=[10, 24])
    parser.add_argument("--attempts", type=int, default=30000)
    parser.add_argument("--phases", nargs="+", choices=["warm", "cold", "compaction"], default=["warm", "cold", "compaction"])
    parser.add_argument("--owned", action="store_true", help="Use TaskModelWait, real control readers and a populated root-fence queue snapshot")
    parser.add_argument("--worker-source", help="Optional archived base source under this launcher's temporary root")
    parser.add_argument("--worker-ref", default="candidate", help="Label for separately measured baseline workers")
    args = parser.parse_args()
    if os.environ.get("OUROBOROS_PYTEST_ACTIVE") != "1":
        parser.error("run through scripts/safe_test.py (isolated synthetic roots required)")
    output = pathlib.Path(os.environ["OUROBOROS_TEST_TEMP_ROOT"]) / "usage-scale"
    output.mkdir()
    if args.worker_source and not pathlib.Path(args.worker_source).resolve().is_relative_to(output.parent):
        parser.error("worker source must be an isolated base archive inside the launcher root")
    source = output / "fixture"
    shape = fixture(source, args.attempts)
    candidate = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                 "diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"])).hexdigest(),
                 "platform": platform.platform(), "python": sys.version, "source": __file__,
                 "fixture": shape, "runs": [], "worker_ref": args.worker_ref,
                 "worker_source": args.worker_source, "owned": args.owned}
    # Pin every runtime/control seam, launch environment and the runner, including
    # platform_layer and source files not yet tracked. Never print credentials.
    candidate["source_sha256"] = {str(path): {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                                       "mode": path.stat().st_mode & 0o777}
        for path in [*pathlib.Path(".").glob("*.py"), *(path for directory in ("ouroboros", "supervisor", "tests", "scripts")
                    for path in pathlib.Path(directory).rglob("*.py"))]}
    candidate["mode_env"] = {key: os.environ.get(key) for key in (
        "OUROBOROS_PYTEST_ACTIVE", "OUROBOROS_REPO_DIR", "OUROBOROS_DATA_DIR", "OUROBOROS_SETTINGS_PATH",
        "OUROBOROS_TEST_TEMP_ROOT", "TOTAL_BUDGET", "OUROBOROS_MODE", "OUROBOROS_RUNTIME_MODE", "OUROBOROS_REVIEW_ENFORCEMENT",
        "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
    candidate["fixture_compactor_source"] = "candidate runner source, including with baseline workers"
    candidate["baseline_compaction_qualification"] = "UNSUPPORTED: baseline workers do not select the baseline fixture/phase compactor" if args.worker_source else "not a baseline run"
    if args.worker_source:
        candidate["worker_source_sha256"] = {str(path.relative_to(args.worker_source)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in pathlib.Path(args.worker_source).rglob("*.py")}
    candidate["scale_qualification_shape"] = (shape["active_rows"] >= 80000 and shape["active_bytes"] >= 128000000
                                                and args.seconds >= 60 and set(args.writers) >= {10, 24}
                                                and set(args.phases) == {"warm", "cold", "compaction"})
    print(json.dumps({"output": str(output), "fixture": shape, "worker_ref": args.worker_ref}), flush=True)
    for writers in args.writers:
        for phase in args.phases:
            name = f"{writers}-{phase}"
            root = output / name / "data"
            shutil.copytree(source, root)
            if args.owned:
                queue_shape = owned_queue(root, writers)
                candidate["owned_queue_shape"] = queue_shape
            result = phase_run(root, output / name / "metrics", writers, phase, args.seconds, args.worker_source, args.owned)
            candidate["runs"].append(result)
            (output / "matrix.json").write_text(json.dumps(candidate, indent=2))
            print(json.dumps({"run": name, "throughput": result["throughput_cycles_sec"],
                              "per_writer_cycles": [item["cycles"] for item in result["per_writer"]],
                              "max_gap": max(item["longest_gap"] for item in result["per_writer"]),
                              "errors": result["errors"]}), flush=True)
    candidate["source_unchanged"] = all(pathlib.Path(path).is_file()
        and hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() == receipt["sha256"]
        and pathlib.Path(path).stat().st_mode & 0o777 == receipt["mode"]
        for path, receipt in candidate["source_sha256"].items())
    (output / "matrix.json").write_text(json.dumps(candidate, indent=2))
    return int(not candidate["source_unchanged"] or any(result["errors"] or any(result["exitcodes"]) for result in candidate["runs"]))


if __name__ == "__main__":
    raise SystemExit(main())
