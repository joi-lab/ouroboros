"""Measured configured-row facts. Publication advances history; inspection only reads.

Producer rows and compact totals share the current store transaction with the
archive cursor. Re-publication replaces one producer, including a late answer.
The window covers instrumented observations, never the unobserved installation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import pathlib

from ouroboros import store
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)
EVENT = "subagent_measurement"
CURSOR = "subagent_metrics_cursor"
PRODUCERS = "subagent_metric_producers"
CALLS = "subagent_metric_call_sources"
COUNTS = ("dispatched", "terminal", "answer_received", "usable_verdict", "no_answer", "pending", "interrupted")


def identity_key(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def set_name(kind, identity):
    """Exportable on every filesystem; public row/task ids remain inside the facts."""
    return kind + "-" + identity_key(str(identity))


def _group(event):
    return set_name("subagent_metrics", event["row_id"]), identity_key(event["row_identity"]) + ":" + event["purpose"]


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _counts(event):
    dispatched, terminal = event.get("dispatched") is True, event.get("terminal") is True
    interrupted = event.get("interrupted") is True
    return {"dispatched": int(dispatched), "terminal": int(terminal),
            "answer_received": int(event.get("answer_received") is True),
            "usable_verdict": int(event.get("usable_verdict") is True),
            "interrupted": int(interrupted), "pending": int(not terminal and (dispatched or event.get("pending") is True)),
            "no_answer": int(dispatched and terminal and not interrupted and event.get("answer_received") is False)}


def fold(prior, event, previous=None):
    """Subtract the prior observation of this immutable producer before adding it."""
    result = copy.deepcopy(prior or {"row_id": event["row_id"], "row_identity": event["row_identity"],
        "purpose": event["purpose"], **dict.fromkeys(COUNTS, 0), "observations": 0,
        "duration_samples": {"count": 0, "sum_sec": 0.0}, "denominators": {"non_interrupted_terminal_dispatches": 0},
        "window": {"from": event.get("ts"), "through": event.get("ts"), "basis": "instrumented_publications"}})
    for row, sign in ((previous, -1), (event, 1)):
        if not row:
            continue
        result["observations"] += sign
        for key, value in _counts(row).items():
            result[key] += sign * value
        result["denominators"]["non_interrupted_terminal_dispatches"] += sign * int(
            row.get("dispatched") is True and row.get("terminal") is True and row.get("interrupted") is not True)
        duration = row.get("duration_sec")
        if number(duration):
            result["duration_samples"]["count"] += sign
            result["duration_samples"]["sum_sec"] += sign * duration
    stamps = [v for v in (result["window"].get("from"), result["window"].get("through"), event.get("ts")) if v]
    result["window"].update({"from": min(stamps) if stamps else None, "through": max(stamps) if stamps else None})
    for key in ("queue", "quota", "nanny_billing"):
        if event.get(key) is not None:
            old = result.get(key) or {}
            if str(event.get("ts") or "") >= str(old.get("observed_at") or ""):
                result[key] = {"observed_at": event.get("ts"), "producer_id": event["producer_id"], "fact": event[key]}
        result.setdefault(key, {"state": "unknown", "reason": "not_observed"})
    result["source_refs"] = {"producer_set": PRODUCERS, "latest_producer_id": event["producer_id"],
                             "latest": event.get("source_refs") or []}
    if event.get("duration_basis"):
        result["duration_basis"] = event["duration_basis"]
    return result


def catch_up(root):
    """Replay only the suffix at a producer boundary; rollback a damaged suffix whole."""
    from ouroboros.swarm_rollup import fold_event_suffix
    from ouroboros.jsonl_tail import JsonlChainSnapshot

    root = pathlib.Path(root)
    try:
        with store.open_store(root, write=True) as txn:
            cursor = txn.meta(CURSOR) or {}
            missing = set(cursor.get("missing_producers") or ())

            def consume(event):
                if event.get("type") in {"llm_round", "llm_api_error"} and event.get("llm_call_id"):
                    txn.put_row(CALLS, str(event["llm_call_id"]), {key: event[key] for key in
                        ("llm_call_id", "task_id", "ledger_attempt_ids", "physical_attempt_id") if key in event})
                    return
                if event.get("type") != EVENT:
                    return
                actor = event.get("actor") or {}
                if actor.get("kind") == "api_call":
                    call = txn.get_row(CALLS, event["producer_id"].removeprefix("call:")) or {}
                    if call.get("ledger_attempt_ids"):
                        actor["usage_ref"] = {"ledger_attempt_ids": call["ledger_attempt_ids"],
                                              "observed_attempt_id": call.get("physical_attempt_id")}
                producer = str(event["producer_id"])
                missing.discard(producer)
                previous = txn.get_row(PRODUCERS, producer)
                if previous == event or previous and previous.get("terminal") is True and event.get("terminal") is False:
                    return
                if previous and _group(previous) != _group(event):
                    raise ValueError("producer identity changed")
                if event.get("row_id"):
                    name, key = _group(event)
                    txn.put_row(name, key, fold(txn.get_row(name, key), event, previous))
                txn.put_row(PRODUCERS, producer, event)
                if event.get("task_id"):
                    txn.put_row(set_name("swarm_actors", event["task_id"]), producer, event)

            through = fold_event_suffix(root, int(cursor.get("through", 0)), consume)
            upper = JsonlChainSnapshot(root / "logs/events.jsonl").upper
            txn.set_meta(CURSOR, {"through": through, "source_root": str(root.resolve()),
                "missing_producers": sorted(missing),
                "coverage": {"state": "partial" if missing else "observed_window", "history_before_window": "unknown",
                             "missing_publications": len(missing), "stream": "complete" if through == upper else "incomplete"}})
    except Exception:
        # A retained projection remains useful, but cannot claim a complete stream.
        with store.open_store(root, write=True) as txn:
            old = txn.meta(CURSOR) or {}
            txn.set_meta(CURSOR, {**old, "coverage": {"state": "unknown", "stream": "unavailable",
                                                    "history_before_window": "unknown"}})
        raise


def publish(root, event):
    """Keep the observation before the derived index; failure never changes execution."""
    if not root or not event.get("producer_id"):
        return
    try:
        event = {**copy.deepcopy(event), "type": EVENT}
        with store.open_store(root, write=False) as txn:
            previous = txn.get_row(PRODUCERS, str(event["producer_id"])) or {}
        if (event.get("actor") or {}).get("kind") == "api_call" and (previous.get("actor") or {}).get("usage_ref"):
            event["actor"].setdefault("usage_ref", copy.deepcopy(previous["actor"]["usage_ref"]))
        if {key: value for key, value in event.items() if key != "ts"} != {
                key: value for key, value in previous.items() if key != "ts"}:
            event["ts"] = event.get("ts") or previous.get("ts") or utc_now_iso()
            if not append_jsonl(pathlib.Path(root) / "logs/events.jsonl", event):
                with store.open_store(root, write=True) as txn:
                    cursor = txn.meta(CURSOR) or {}
                    missing = sorted(set(cursor.get("missing_producers") or ()) | {event["producer_id"]})
                    txn.set_meta(CURSOR, {**cursor, "missing_producers": missing,
                        "coverage": {**cursor.get("coverage", {}), "state": "partial", "missing_publications": len(missing)}})
                raise OSError("measurement journal append failed")
        catch_up(root)
        from ouroboros.swarm_receipt import refresh_for_task
        refresh_for_task(root, event.get("task_id"))
    except Exception:
        log.warning("subagent measurement projection unavailable", exc_info=True)


def read(root, row_id, *, row_identity=None, purpose=None):
    """No history read, provider call or write on a model round."""
    unknown = {"state": "unknown", "stream": "not_observed", "history_before_window": "unknown"}
    try:
        with store.open_store(root, write=False) as txn:
            coverage = (txn.meta(CURSOR) or {}).get("coverage", unknown)
            if row_identity is not None:
                purposes = (purpose,) if purpose else ("review", "plan", "execution")
                rows = [txn.get_row(set_name("subagent_metrics", row_id), identity_key(row_identity) + ":" + p) for p in purposes]
            else:
                rows = list(txn.set_rows(set_name("subagent_metrics", row_id)).values())
        return {"coverage": coverage, "rows": [{**row, "coverage": coverage} for row in rows
                                                if row and (not purpose or row["purpose"] == purpose)]}
    except Exception:
        return {"coverage": {**unknown, "stream": "unavailable"}, "rows": []}


def fact_line(task, *, drive_root):
    """A compact dynamic fact for this task's selected row, outside cached catalogs."""
    from ouroboros.subagent_history import execution_identity

    snapshot = task.get("configured_subagent") or {}
    row_id = str(snapshot.get("selected_subagent_id") or "")
    if not row_id:
        return ""
    facts = read(drive_root, row_id, row_identity=execution_identity(snapshot))
    if not facts["rows"]:
        return ""
    return "Subagent measurements (observed window; earlier history unknown): " + json.dumps(
        [{key: row[key] for key in ("purpose", "dispatched", "terminal", "answer_received", "usable_verdict",
                                    "no_answer", "pending", "interrupted", "coverage")} for row in facts["rows"]],
        separators=(",", ":"))
