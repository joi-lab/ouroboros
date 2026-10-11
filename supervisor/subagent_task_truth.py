"""Delegation-truth enrichment for the subagent ``task_done`` transport frame.

Split from ``supervisor/events.py`` at its byte ceiling: the PUSHED (log
channel) ``task_done`` event is stamped with the same delegation truth the
chat frame carries — additive keys, applied only AFTER the durable
``events.jsonl`` append (transport enrichment, never a log-shape change) — so
a card whose chat frame never arrives still upgrades its executor chip from
the task_done log event instead of staying at the dispatch-only label.
"""

from __future__ import annotations

from typing import Any, Dict


def enrich_task_done_event(
    task_done_event: Dict[str, Any], effective_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Stamp executor_route/execution_evidence/actual_substrate; return the envelope."""
    envelope = effective_result.get("subagent_envelope")
    envelope = envelope if isinstance(envelope, dict) else {}
    if str(effective_result.get("executor_route") or ""):
        task_done_event.setdefault(
            "executor_route", str(effective_result["executor_route"]))
    if isinstance(envelope.get("execution_evidence"), dict):
        task_done_event.setdefault(
            "execution_evidence", envelope["execution_evidence"])
    if envelope.get("actual_substrate"):
        task_done_event.setdefault(
            "actual_substrate", str(envelope["actual_substrate"]))
    return envelope
