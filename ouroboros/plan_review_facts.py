"""Host-recorded plan-review facts for the learning surfaces (reflection, late settlement).

The post-task reflection asks the mind to account for every review finding, yet its
evidence held only the commit/advisory ledger and the acceptance panels: what the PLAN
review found, how the author answered, whether the element survived in the plan the
author finally selected, and which reviewers never answered all vanished at task end.
This leaf reads the recorded plan-review state and projects those facts, bounded and
with a source pointer. It scores nothing and decides nothing: which advice mattered is
the mind's judgement (BIBLE P5/P13); the host only keeps the facts in front of it.

One builder for the whole slice (``plan_review_reflection_slice``), one pure core over
an already-loaded state (``facts_from_state``) so tests need no drive, and one bounded
row for a panel that settled after the task ended (``late_settlement_reflection_entry``).
Every cut the fit makes is counted in ``omitted``; an unreadable recorded source is a
named ``unavailable`` fact, never rendered as "nothing was there".
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict, List, Mapping, Optional

from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

# Fit limits for a Light-route prompt section and a context row, not judgements.
PLAN_REVIEW_REFLECTION_CHARS = 6_000
LATE_SETTLEMENT_ROW_CHARS = 1_500
_TEXT_CHARS = 200
_WAVES_SHOWN = 8
_CLOSURE_NOTES_SHOWN = 6
_LATE_ROW_ELEMENTS_SHOWN = 6
LATE_SETTLEMENT_TASK_TYPE = "acceptance_late_settlement"
_SECTION_HEADING = "TASK PLAN REVIEW (host-recorded facts; which advice mattered is yours to judge):\n"


def _cut(value: Any, limit: int) -> str:
    """One field bounded by the shared strict helper (the omission marker rides inside the
    budget); whole-row cuts are counted in ``omitted``."""
    from ouroboros.utils import truncate_within_limit

    return truncate_within_limit(" ".join(str(value or "").split()), limit)


def _spec_texts(spec: Mapping[str, Any]) -> Dict[str, str]:
    """``id -> identifying text`` for every id-bearing element of a normalized spec, goal included."""
    try:
        from ouroboros.tools.plan_spec import _text_by_id

        texts = dict(_text_by_id(spec))
    except Exception:
        texts = {}
    goal = str(spec.get("goal") or "")
    if goal:
        texts.setdefault("goal", goal)
    return texts


def _dispositions_by_finding(wave: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Every stored answer per finding id. Across recorder calls a later answer supersedes the
    earlier one at write time, so two entries for one id can only come from ONE call — the
    contradiction the closure table refuses (the finding stays open)."""
    answers: Dict[str, List[Dict[str, Any]]] = {}
    for item in wave.get("dispositions") or []:
        fid = str(item.get("finding_id") or "").strip() if isinstance(item, Mapping) else ""
        if fid:
            answers.setdefault(fid, []).append(dict(item))
    return answers


def _answer_text(answers: List[Dict[str, Any]]) -> str:
    if not answers:
        return "unanswered"
    if len(answers) > 1:
        return (f"contradictory answers in one call ({len(answers)}: "
                + ", ".join(str(a.get("decision") or "?") for a in answers) + "); the finding stays open")
    return f"{answers[0].get('decision')}: {_cut(answers[0].get('rationale'), _TEXT_CHARS)}"


def _element_fates(critic_texts: Mapping[str, str], selected_texts: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """``id -> same|changed|removed|renumbered→<id>`` for the reviewed plan's elements, plus
    ``selected:<id> -> added`` for selected elements no reviewed element accounts for.

    Ids are positional, so dropping one element shifts its neighbours. Each reviewed element
    claims at most ONE selected occurrence (same id and text first, then the same text under
    another id = ``renumbered``); a reviewed text that no selected occurrence carries is
    ``removed`` — also when a moved neighbour now occupies its id — unless its id kept a text
    new to the plan (``changed``). Every selected occurrence left unclaimed is ``added``, under
    its own ``selected:`` key so a reused id never hides it. The goal is compared like any element.
    """
    if selected_texts is None:
        return {}
    unclaimed: Dict[str, str] = dict(selected_texts)
    critic_values = set(critic_texts.values())
    fates: Dict[str, str] = {}
    for eid, text in critic_texts.items():
        if unclaimed.get(eid) == text:
            fates[eid] = "same"
            unclaimed.pop(eid)
            continue
        moved = next((sid for sid, stext in unclaimed.items() if text and stext == text and sid != eid), None)
        if moved is not None:
            fates[eid] = f"renumbered→{moved}"
            unclaimed.pop(moved)
            continue
        selected = selected_texts.get(eid)
        if selected is None or selected in critic_values:
            fates[eid] = "removed"
        else:
            fates[eid] = "changed"
            unclaimed.pop(eid, None)
    for sid in unclaimed:
        fates[f"selected:{sid}"] = "added"
    return fates


def _wave_summary(wave: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "cycle": wave.get("cycle_index"),
        "aggregate": wave.get("aggregate"),
        "closed": bool(wave.get("closed")),
        "counts": wave.get("counts") if isinstance(wave.get("counts"), Mapping) else {},
    }


def _selected_plan(author_plan: Mapping[str, Any], critic_spec: Mapping[str, Any]) -> tuple[Dict[str, Any], Dict[str, str]]:
    author = author_plan.get("author_disposition") if isinstance(author_plan.get("author_disposition"), Mapping) else {}
    selected_spec = author_plan.get("spec") if isinstance(author_plan.get("spec"), Mapping) else {}
    overview: Any = "not computed"
    try:
        from ouroboros.tools.plan_spec import spec_delta

        delta = spec_delta(dict(critic_spec), dict(selected_spec)) if critic_spec and selected_spec else None
        if isinstance(delta, Mapping):
            lists = delta.get("lists") if isinstance(delta.get("lists"), Mapping) else {}
            overview = {
                **{name: list((delta.get("ids") or {}).get(name) or []) for name in ("added", "removed", "changed")},
                "renumbered": [{"from": m.get("from"), "to": m.get("to")} for m in (delta.get("renumbered") or [])
                               if isinstance(m, Mapping)],
                "goal_changed": bool(delta.get("goal_changed")),
                "invariants": {name: len((lists.get("invariants") or {}).get(name) or []) for name in ("added", "removed")},
            }
    except Exception:
        log.debug("plan facts: spec delta unavailable", exc_info=True)
    return ({
        "author_action": str(author.get("action") or ""),
        "disposition": str(author.get("disposition") or author.get("verdict") or ""),
        "rationale": _cut(author.get("rationale"), 300),
        "delta": overview,
    }, _spec_texts(selected_spec))


def facts_from_state(
    state: Mapping[str, Any],
    *,
    critic: Optional[Mapping[str, Any]],
    author_plan: Optional[Mapping[str, Any]] = None,
    author_plan_unavailable: str = "",
    claims_source: str = "",
    census: Optional[Mapping[str, Any]] = None,
    source_ref: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Pure projection of one task's plan-review record (no drive, no model call).

    ``critic`` is the wave whose findings the author answered (the reviewed plan);
    ``author_plan`` the plan the author selected without a new wave, if any, and
    ``author_plan_unavailable`` the reason a recorded one could not be read (named,
    never rendered as "no author plan"); ``census`` the critic wave's slot census
    (``plan_wave_slot_census``). Returns ``None`` when the task recorded no wave at all.
    """
    waves = [w for w in (state.get("waves") or []) if isinstance(w, Mapping)]
    if not waves and not isinstance(critic, Mapping):
        return None
    critic = critic if isinstance(critic, Mapping) else waves[-1]
    spec = critic.get("spec") if isinstance(critic.get("spec"), Mapping) else {}
    texts = _spec_texts(spec)
    answers = _dispositions_by_finding(critic)
    selected: Optional[Dict[str, Any]] = None
    selected_texts: Optional[Dict[str, str]] = None
    if author_plan_unavailable:
        selected = {"unavailable": str(author_plan_unavailable)}
    elif isinstance(author_plan, Mapping):
        selected, selected_texts = _selected_plan(author_plan, spec)
    fates = _element_fates(texts, selected_texts)
    findings_by_element: Dict[str, List[Dict[str, Any]]] = {}
    questions: List[Dict[str, Any]] = []
    for finding in critic.get("findings") or []:
        if not isinstance(finding, Mapping):
            continue
        fid = str(finding.get("finding_id") or finding.get("id") or "")
        row = {
            "finding_id": fid,
            "class": str(finding.get("class") or ""),
            "model": str(finding.get("model") or finding.get("slot") or ""),
            "summary": _cut(finding.get("summary"), 160),
            "disposition": _answer_text(answers.get(fid, [])),
        }
        element = str(finding.get("breaks") or "").strip()
        findings_by_element.setdefault(element or "(no element)", []).append(row)
        if row["class"] == "need_evidence" and not str(finding.get("locator") or "").strip() and element:
            questions.append({"finding_id": fid, "breaks": element, "question": row["summary"], "answer": row["disposition"]})
    touched = set(findings_by_element) | {eid for eid, fate in fates.items() if fate != "same"}

    def fate(eid: str) -> str:
        if eid in fates:
            return fates[eid]
        if eid in texts:
            return "same" if selected_texts is not None else "n/a"
        return "not an element of the reviewed plan"

    def text_of(eid: str) -> str:
        if eid.startswith("selected:"):
            return (selected_texts or {}).get(eid[len("selected:"):], "")
        return texts.get(eid, "")

    elements = [{
        "id": eid, "text": _cut(text_of(eid), _TEXT_CHARS), "changed_in_selected_plan": fate(eid),
        "findings": findings_by_element.get(eid, []),
    } for eid in sorted(touched, key=lambda e: (e == "(no element)", e))]
    unresolved: Dict[str, Any] = {}
    if isinstance(census, Mapping):
        rows = {name: [r for r in (census.get(name) or []) if isinstance(r, Mapping)]
                for name in ("awaiting", "unresolved", "uncollected")}
        unresolved = {**{name: len(group) for name, group in rows.items()},
                      "slots": [str(r.get("slot_id") or "") for group in rows.values() for r in group]}
    closure_notes = list(critic.get("closure_notes") or [])
    facts: Dict[str, Any] = {
        "source_ref": dict(source_ref or {}),
        "wave_ref": critic.get("wave_artifact") if isinstance(critic.get("wave_artifact"), Mapping) else {},
        "scope": "elements and questions come from the reviewed wave; earlier waves appear in waves[] by their counts only",
        "claims_source": claims_source or "unknown",
        "waves": [_wave_summary(w) for w in waves[-_WAVES_SHOWN:]],
        "waves_total": len(waves),
        "reviewed_plan": {"aggregate": critic.get("aggregate"), "closed": bool(critic.get("closed")),
                          "cycle": critic.get("cycle_index"), "closure_notes": closure_notes[:_CLOSURE_NOTES_SHOWN],
                          "closure_notes_total": len(closure_notes)},
        "selected_plan": selected,
        "elements": elements,
        "unchanged_elements_without_findings": len([eid for eid in texts if eid not in touched]),
        "questions": questions,
        "reviewers_without_a_merged_answer": unresolved,
    }
    return _fit(facts)


def _fit(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Bound the rendered section (heading included): note-class finding texts go first, then
    trailing elements, questions, unresolved slot names and the rationale; every cut is counted
    in ``omitted``, which is part of what is measured so the bound holds with it present."""
    def over() -> bool:
        return len(render_plan_review_section(facts)) > PLAN_REVIEW_REFLECTION_CHARS

    if not over():
        return facts
    omitted = {"note_findings_summaries": 0, "elements": 0, "questions": 0, "unresolved_slots": 0,
               "note": "whole rows omitted; read source_ref"}
    facts["omitted"] = omitted
    for element in facts["elements"]:
        for row in element["findings"]:
            if row.get("class") == "note" and "summary" in row:
                del row["summary"]
                omitted["note_findings_summaries"] += 1
    while over() and facts["elements"]:
        facts["elements"].pop()
        omitted["elements"] += 1
    while over() and facts["questions"]:
        facts["questions"].pop()
        omitted["questions"] += 1
    slots = (facts.get("reviewers_without_a_merged_answer") or {}).get("slots")
    while over() and slots:
        slots.pop()
        omitted["unresolved_slots"] += 1
    selected = facts.get("selected_plan") if isinstance(facts.get("selected_plan"), dict) else None
    if over() and selected and isinstance(selected.get("delta"), dict):
        # The delta's id lists are variable-length too: collapse them to counts, keeping the booleans.
        selected["delta"] = {key: (len(value) if isinstance(value, list) else value) for key, value in selected["delta"].items()}
        omitted["delta_id_lists_collapsed_to_counts"] = True
    if over() and selected and selected.get("rationale"):
        selected["rationale"] = _cut(selected["rationale"], 80)
    if over():  # last resort: the wave history to its newest two rows and the notes to a count
        waves = facts.get("waves") or []
        omitted["waves"] = max(0, len(waves) - 2)
        facts["waves"] = waves[-2:]
        notes = facts.get("reviewed_plan", {}).get("closure_notes")
        if isinstance(notes, list):
            facts["reviewed_plan"]["closure_notes"] = []
    return facts


def _render(facts: Mapping[str, Any]) -> str:
    return json.dumps(facts, ensure_ascii=False, indent=2, default=str)


def render_plan_review_section(facts: Mapping[str, Any]) -> str:
    """The prompt section the reflection reads first: facts, with the judgement left to the reader."""
    return _SECTION_HEADING + _render(facts)


def plan_review_reflection_slice(root: Any, task_id: str, *, task: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Load one task's plan-review record and project it; ``None`` without waves, a disclosed
    ``{"unavailable": …}`` when a recorded source cannot be read or the wave the author
    answered is not in the index (no other wave is substituted for it)."""
    from ouroboros.task_results import current_plan_review_wave, load_plan_review_state, plan_review_wave
    from ouroboros.tools.plan_review_artifacts import PlanReviewSourceUnavailable, authority_wave, current_author_plan

    if not str(task_id or "").strip():
        return None
    source_ref = {"kind": "task_result", "reader": "get_task_result", "task_id": str(task_id), "field": "plan_review_state"}
    drive = pathlib.Path(str(root))

    def unavailable(reason: Any) -> Dict[str, Any]:
        return {"unavailable": str(reason), "source_ref": source_ref}

    try:
        state = load_plan_review_state(drive, str(task_id))
    except (PlanReviewSourceUnavailable, OSError, ValueError) as exc:
        return unavailable(exc)
    if not (state.get("waves") or []):
        return None
    author_plan_unavailable = ""
    try:
        author_plan = current_author_plan(drive, str(task_id), state)
    except (PlanReviewSourceUnavailable, OSError, ValueError) as exc:
        author_plan, author_plan_unavailable = None, str(exc)
    try:
        if isinstance(author_plan, Mapping) and author_plan.get("review_fingerprint"):
            hot = plan_review_wave(state, str(author_plan["review_fingerprint"]))
            if hot is None:
                return unavailable(f"PLAN_REVIEW_SOURCE_UNAVAILABLE: the reviewed wave the author answered "
                                   f"({str(author_plan['review_fingerprint'])[:12]}…) is not in the index")
        else:
            hot = current_plan_review_wave(state) or (state.get("waves") or [None])[-1]
        critic = authority_wave(drive, str(task_id), hot) or hot
    except (PlanReviewSourceUnavailable, OSError, ValueError) as exc:
        return unavailable(exc)
    claims_source = ""
    try:
        from ouroboros.review_evidence_sections import _accept_effective_claims

        contract = (task or {}).get("task_contract") if isinstance((task or {}).get("task_contract"), Mapping) else {}
        claims_source = _accept_effective_claims(None, dict(contract or {}), drive, str(task_id))[1]
    except Exception:
        log.debug("plan facts: claims source unavailable", exc_info=True)
    census = None
    try:
        from ouroboros.tools.plan_review_runtime import plan_wave_slot_census

        census = plan_wave_slot_census(dict(critic)) if isinstance(critic, Mapping) else None
    except Exception:
        log.debug("plan facts: slot census unavailable", exc_info=True)
    return facts_from_state(state, critic=critic, author_plan=author_plan, author_plan_unavailable=author_plan_unavailable,
                            claims_source=claims_source, census=census, source_ref=source_ref)


def _late_panel(result: Mapping[str, Any], retry_key: str) -> Optional[Dict[str, Any]]:
    projection = result.get("review_projection") if isinstance(result.get("review_projection"), Mapping) else {}
    for panel in projection.get("panels") or []:
        late = panel.get("late_settlement") if isinstance(panel, Mapping) else None
        subject = late.get("reviewed_subject") if isinstance(late, Mapping) and isinstance(late.get("reviewed_subject"), Mapping) else {}
        if isinstance(late, Mapping) and str(subject.get("retry_key") or "") == str(retry_key):
            return dict(panel)
    return None


def late_settlement_reflection_entry(root: Any, task_id: str, retry_key: str) -> Optional[Dict[str, Any]]:
    """One bounded reflection row for a panel that settled after its task ended.

    The reflection that already ran was written over a still-running panel; this row
    puts the settled verdict (the host's own sentence), each reviewer's output and the
    plan findings of the touched elements into the same log, so the next tasks read it
    through the ordinary recent-reflections window. No model call, no marker: the
    Pattern Register stays closed (``_admits_pattern_register`` reads error evidence).
    The source pointer comes right after the verdict, so a bounded row never loses it.
    """
    from ouroboros.task_results import load_task_result
    from ouroboros.utils import truncate_within_limit

    drive = pathlib.Path(str(root))
    result = load_task_result(drive, str(task_id)) or {}
    panel = _late_panel(result, retry_key) if isinstance(result, Mapping) else None
    if panel is None:
        return None
    late = panel.get("late_settlement") or {}
    note = str(late.get("note") or "")
    lines = [_cut(note.splitlines()[0] if note else "acceptance settled late", 400),
             f"Source: get_task_result(task_id={task_id}), review_projection panel {panel.get('panel_id') or '?'}."]
    for row in late.get("reviewer_outputs") or []:
        if isinstance(row, Mapping) and row.get("slot_id"):
            who = str(row.get("model") or row.get("requested_model") or "")
            lines.append(f"- {row['slot_id']}{f' ({who})' if who else ''}: {row.get('verdict') or row.get('operation_state') or 'unknown'}")
    try:
        facts = plan_review_reflection_slice(drive, str(task_id), task=result)
    except Exception as exc:  # the row is still worth writing without the plan facts
        facts = {"unavailable": str(exc)}
    if isinstance(facts, Mapping) and facts.get("unavailable"):
        lines.append(f"Plan-review facts unavailable: {_cut(facts['unavailable'], 200)}")
    elif isinstance(facts, Mapping) and facts.get("elements"):
        shown = facts["elements"][:_LATE_ROW_ELEMENTS_SHOWN]
        lines.append(f"Plan review ({facts.get('claims_source')} claims; {len(shown)} of {len(facts['elements'])} elements): "
                     + "; ".join(f"{e['id']} {e['changed_in_selected_plan']}, "
                                 + ", ".join(f"{f['class']} {f['disposition'].split(':', 1)[0]}" for f in e["findings"])
                                 for e in shown))
    return {
        "ts": utc_now_iso(), "task_id": str(task_id), "type": LATE_SETTLEMENT_TASK_TYPE,
        "task_type": LATE_SETTLEMENT_TASK_TYPE, "supplement_id": f"acceptance-late:{retry_key}",
        "goal": _cut(result.get("text") or result.get("description") or "", 200),
        "reflection": truncate_within_limit("\n".join(lines), LATE_SETTLEMENT_ROW_CHARS),
        "source_ref": {"kind": "task_result", "reader": "get_task_result", "task_id": str(task_id),
                       "field": "review_projection", "panel_id": panel.get("panel_id")},
    }


def learn_from_late_settlement(root: Any, result: Mapping[str, Any], retry_key: str) -> bool:
    """Append the late-settlement row through the ordinary routed writer; failures only log."""
    from types import SimpleNamespace

    from ouroboros.reflection import append_reflection_routed

    try:
        root = pathlib.Path(str(root))
        entry = late_settlement_reflection_entry(root, str(result.get("task_id") or result.get("id") or ""), retry_key)
        if entry is None:
            return False
        append_reflection_routed(SimpleNamespace(drive_root=root), dict(result), entry)
        return True
    except Exception:
        log.warning("late settlement reflection row was not written", exc_info=True)
        return False
