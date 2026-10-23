"""Fan-out facts on the emitting task row, advanced over the event chain once."""

import copy
import json
import pathlib

from ouroboros.jsonl_tail import JsonlChainSnapshot
from ouroboros.utils import jsonl_chain_handles


def initial_swarm_rollup(drive_root):
    try:
        source = JsonlChainSnapshot(pathlib.Path(drive_root) / "logs/events.jsonl")
        end = source.upper if not source.upper or source._read(source.upper - 1, source.upper) == b"\n" else 0
        return {"source_root": str(pathlib.Path(drive_root).resolve()), "through": end}
    except OSError:
        return {}  # Admission can proceed; the observation retries its history later.


def load_swarm_rollup(drive_root, task_id):
    """Catch up from the last complete line, including a failed producer update.

    Legacy rows replay once. Rotation preserves chain offsets; missing or torn
    history refuses a partial rollup. A concurrent publisher cannot move the
    cursor backwards, and finalization never invents a missing task row.
    """
    from ouroboros.task_results import load_task_result, write_task_result

    root = pathlib.Path(drive_root)
    row = load_task_result(root, task_id, strict=True) or {}
    prior = row.get("swarm_fanout_rollup")
    facts = copy.deepcopy(prior) if isinstance(prior, dict) and prior.get("source_root") == str(root.resolve()) else {}
    children, lanes = set(facts.get("child_ids", [])), list(facts.get("lanes_requested", []))
    through = fold_event_suffix(root, facts.get("through", 0),
                                lambda event: _accumulate(facts, children, lanes, event, task_id))
    facts.update(source_root=str(root.resolve()), through=through, child_ids=sorted(children), lanes_requested=lanes)
    if row and facts != prior:
        def publish(current, fields):
            if current.get("swarm_fanout_rollup") != prior:
                return None
            return {**fields, "status": current["status"], "swarm_fanout_rollup": facts}
        write_task_result(root, task_id, row["status"], _field_projector=publish, strict_existing_dict=True)
    return facts


def fold_event_suffix(root, through, consume):
    """Visit complete unconsumed lines; the caller commits effects and cursor together."""
    source = JsonlChainSnapshot(pathlib.Path(root) / "logs/events.jsonl")
    if through > source.upper:
        raise ValueError("swarm event chain shortened")
    for index in range(len(source.entries)):
        _, end = source.segment(index)
        if through >= end:
            continue
        with jsonl_chain_handles(source.path, strict=True, start_offset=through, snapshot=source.snapshot) as handles:
            handle = handles[0][1]
            while through < end:
                raw = handle.readline(end - through)
                if not raw.endswith(b"\n"):
                    if source.entries[index][2]:
                        break
                    raise ValueError("swarm event history is incomplete")
                through += len(raw)
                if not raw.strip():
                    continue
                event = json.loads(raw)
                if isinstance(event, dict):
                    consume(event)
    return through


def _accumulate(facts, children, lanes, event, task_id):
    if event.get("type") != "swarm_fanout" or str(event.get("parent_task_id") or event.get("task_id") or "") != task_id:
        return
    facts["fanout_count"] = facts.get("fanout_count", 0) + 1
    for key, value, convert in (("requested_count", event.get("requested_count"), int),
                                 ("fanout_interval_sec_total", event.get("fanout_interval_sec", event.get("inter_wave_latency_sec")), float)):
        try:
            facts[key] = facts.get(key, 0) + convert(value or 0)
        except (ValueError, TypeError):
            pass
    children.update(str(tid) for tid in event.get("task_ids") or [] if str(tid or "").strip())
    lane = str(event.get("requested_model_lane") or "").strip()
    if lane and lane not in lanes:
        lanes.append(lane)
