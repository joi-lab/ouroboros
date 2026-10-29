"""The episode of a lost Max: one owner-held row, a line in Main when it opens and when it closes.

Every fresh start of an integrating focus (a view role not delegated as a child or nanny) on the owner's
Max with a known window carries the host's measure of its Max room (``memory_floor.with_max_room``). A
start lowered to Low while the books, core, assignment, reply reserve and work room alone fit the window
(``crowded``: memory or tool schemas took the room) opens the episode: the ``episode`` row of the
``memory_max`` obligation set and one line in Main. Later such starts only update its facts: the largest
work room and the smallest window that lost Max, the last measure. A route whose window cannot hold even
that floor (``route``) is a disclosed fact of its task, never an episode, and closes none. The episode
closes at a fresh integrating start that holds whole books beside my whole story and whose own need would
hold them in the worst lost frame too (the smallest lost window with its reply reserve and work room per
level, at the most levels a lost start had), so a raised window or a Main turn closes it by measure; or at
one whose owner mode is no longer Max. One line in Main says which. A running Low task's view was captured at its start: it neither holds
nor closes the episode. While it is open, an integrating reader's Health names it (``health_line``).

The two lines go through the terminal outbox (``supervisor.terminal_delivery``) under ids the row holds
before the send. A fresh integrating start finds a line not yet delivered and no longer owed and sends it
again under the same id (the outbox dedupes); a delivered close line removes the row, and a new episode
takes a closed one's row only once its lines are delivered or held by the outbox. No walker, no outbox of
its own; the set is one row.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from ouroboros import memory_floor as floor, store
from ouroboros.chronicle_store import DELEGATED_FOCUS_ROLES
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

SET, IDENTITY = "memory_max", "episode"
NOTICE_TYPE = "memory_max_notice"
_NUMBERS = ("window", "allowance", "floor_need", "schemas", "zero_need", "need", "story", "room", "unfolded",
            "reserve", "work", "levels", "folds", "parts", "headroom", "short")


def start_fact(plan: Any, mode: str) -> Optional[Dict[str, Any]]:
    """An integrating focus's fresh start: its settled Max room, ``{"cause": "owner", "mode": …}`` when the
    owner's mode is not Max, else ``None`` (a delegated child or nanny, no view, an unknown window)."""
    facts = (getattr(plan.projection(mode), "memory_facts", None) or {}) if plan is not None else {}
    if not facts.get("role") or facts["role"] in DELEGATED_FOCUS_ROLES:
        return None
    owner = str(getattr(plan, "preferred_mode", "") or "")
    if owner != "max":
        return {"cause": "owner", "mode": owner}
    return dict(facts["max_room"]) if (facts.get("max_room") or {}).get("cause") else None


def _numbers(fact: Mapping[str, Any], **extra: Any) -> Dict[str, Any]:
    return {**{key: fact[key] for key in _NUMBERS if key in fact}, **extra}


def _frame(fact: Mapping[str, Any]) -> Dict[str, int]:
    """This start's frame: the work room one level takes and the reply reserve."""
    return {"unit": fact["work"] // max(1, fact["levels"]), "reserve": fact["reserve"]}


def transition(row: Optional[Mapping[str, Any]], fact: Mapping[str, Any], *, task_id: str,
               now: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(row, event)`` after one fresh integrating start: ``opened``, ``updated``, ``closed`` or ``""``."""
    current = dict(row) if row else None
    open_ = current is not None and not current.get("closed")
    cause = fact["cause"]
    if cause == "crowded" and not open_:
        episode = f"{now}:{task_id}"
        return {"id": episode, "opened_at": now, "task": task_id, "starts": 1, "window": fact["window"],
                **_frame(fact), "levels": fact["levels"], "first": _numbers(fact),
                "last": _numbers(fact, at=now, task=task_id),
                "notices": {"open": {"id": f"memory_max:{episode}:open", "recorded": False}}}, "opened"
    if cause == "crowded":
        smaller = fact["window"] < current["window"]
        return {**current, "starts": current["starts"] + 1, **(_frame(fact) if smaller else {}),
                "window": fact["window"] if smaller else current["window"], "levels": max(current["levels"], fact["levels"]),
                "last": _numbers(fact, at=now, task=task_id)}, "updated"
    # This start's need with the worst lost frame's reply reserve and work room, against that frame's window.
    held = cause == "held" and open_ and (fact["need"] - fact["work"] - fact["reserve"] + current["reserve"]
                                          + current["unit"] * current["levels"]) <= current["window"]
    if held or (cause == "owner" and open_):
        return {**current, "closed": _numbers(fact, at=now, task=task_id, cause=cause, mode=fact.get("mode", "max")),
                "notices": {**current["notices"], "close": {"id": f"memory_max:{current['id']}:close",
                                                          "recorded": False}}}, "closed"
    return current, ""


def _minute(stamp: str) -> str:
    return str(stamp or "")[:16].replace("T", " ")


def folds_by(root: Any, since: str, until: str) -> Optional[Dict[str, int]]:
    """Parts and accounts published in ``[since, until]`` by author kind (``mind``; ``helper``: Light drafts,
    children, nannies), a later decision on a draft aside; ``None`` when the chronicle is unread."""
    try:
        from ouroboros.chronicle_store import ChronicleStore

        records = ChronicleStore(root).records(kinds=("part", "account"))
    except Exception:
        log.debug("Chronicle unreadable for the Max episode's close line", exc_info=True)
        return None
    by: Dict[str, int] = {}
    for record in records:
        if since <= str(record.get("ts") or "") <= until:
            kind = str((record.get("author") or {}).get("kind") or "")
            by[kind] = by.get(kind, 0) + 1
    return by


def notice_text(row: Mapping[str, Any], which: str, by: Optional[Mapping[str, int]] = None) -> str:
    """The one place the two Main lines are worded: the host's measure and the possibility, never a verdict.
    ``by``: the close line's folds while the episode was open, by author kind (``folds_by``)."""
    first, starts = row["first"], f"{row['starts']} Low start{'' if row['starts'] == 1 else 's'}"
    if which == "open":
        return (f"Max lost: task {row['task']} started in Low with chapter maps. The host's measure: "
                f"{floor.measure_text(first)}. {floor.CROWDED_POSSIBILITY} Later Low starts stay in this episode; "
                "a line here will say when it closes.")
    closed = row["closed"]
    span = f"Max was lost from {_minute(row['opened_at'])} to {_minute(closed['at'])} over {starts}"
    if closed["cause"] == "owner":
        return f"The lost-Max episode closed: the owner selected {closed['mode'].capitalize()} (task {closed['task']}). {span}."
    folded = "" if by is None else (f"; parts and accounts written meanwhile: {by.get('mind', 0)} by me, "
                                     f"{by.get('helper', 0)} by helpers")
    return (f"Max holds again: task {closed['task']} started with whole books beside my whole story ("
            f"{floor.measure_text(closed)}). {span}; my story went from {first['story']:,} to {closed['story']:,} "
            f"tokens{folded}.")


def health_line(root: Any, role: str = "integrator") -> str:
    """One WARNING for an integrating reader while the episode is open: one row read, no usage scan."""
    if role in DELEGATED_FOCUS_ROLES:
        return ""
    try:
        with store.open_store(root, write=False) as txn:
            row = txn.get_row(SET, IDENTITY)
    except Exception:
        log.debug("Max episode unreadable for Health", exc_info=True)
        return ""
    if not row or row.get("closed"):
        return ""
    last = row["last"]
    return (f"WARNING: MAX LOST since {_minute(row['opened_at'])} (task {row['task']}; {row['starts']} Low "
            f"start{'' if row['starts'] == 1 else 's'}). At the last one (task {last['task']}): "
            f"{floor.measure_text(last)}. {floor.CROWDED_POSSIBILITY}")


def memory_can_help(root: Any) -> bool:
    """An open episode whose last Low start had room for the books without memory (``zero_need`` within the
    window): folding memory can bring Max back. Tool schemas that tip it are not memory's to fold."""
    try:
        with store.open_store(root, write=False) as txn:
            row = txn.get_row(SET, IDENTITY)
    except Exception:
        log.debug("Max episode unreadable for the helper", exc_info=True)
        return False
    last = (row or {}).get("last") or {}
    return bool(row) and not row.get("closed") and "zero_need" in last and last["zero_need"] <= last["window"]


def _held(root: Any, delivery_id: str) -> bool:
    """The outbox delivered this line or holds it as owed (an unregistered live send is neither)."""
    from supervisor import terminal_delivery as outbox

    return outbox.already_delivered(root, delivery_id) or delivery_id in {
        str(item.get("delivery_id") or "") for item in outbox.pending_deliveries(root)}


def _publish(root: Any, row: Dict[str, Any], event_queue: Any) -> bool:
    """Send each line not yet delivered and no longer owed; mark delivered ones. True when the row changed."""
    from supervisor import terminal_delivery as outbox
    from supervisor.schedule_notes import owner_chat_id

    changed, owed = False, None
    for which in ("open", "close"):
        notice = row["notices"].get(which)
        if not notice or notice.get("recorded"):
            continue
        if outbox.already_delivered(root, notice["id"]):
            notice["recorded"], changed = True, True
            continue
        if owed is None:
            owed = {str(item.get("delivery_id") or "") for item in outbox.pending_deliveries(root)}
        if notice["id"] not in owed:
            closed = row.get("closed") or {}
            by = folds_by(root, row["opened_at"], closed["at"]) if which == "close" and closed.get("cause") == "held" else None
            outbox.enqueue_terminal_delivery_outcome(root, {
                "type": "send_message", "chat_id": owner_chat_id(root), "task_id": "", "text": notice_text(row, which, by),
                "role": "system", "system_type": NOTICE_TYPE, "delivery_id": notice["id"]}, event_queue=event_queue)
    return changed


def observe_task_start(ctx: Any, plan: Any, mode: str, *, task_id: str, event_queue: Any = None) -> str:
    """``observe_start`` on the installation's canonical data root (a child execution drive holds no episode)."""
    try:
        from ouroboros.tool_access_paths import canonical_data_root

        root = canonical_data_root(ctx)
    except Exception:
        root = getattr(ctx, "drive_root", None)
    return observe_start(root, plan, mode, task_id=task_id, event_queue=event_queue) if root else ""


def observe_start(root: Any, plan: Any, mode: str, *, task_id: str, event_queue: Any = None) -> str:
    """One fresh start against the episode; never raises (the task's start outranks this fact)."""
    try:
        fact = start_fact(plan, mode)
        if fact is None or fact["cause"] == "route":
            return ""
        with store.open_store(root, write=False) as txn:
            row = txn.get_row(SET, IDENTITY)
        if row is None and fact["cause"] != "crowded":
            return ""
        if row and row.get("closed") and fact["cause"] == "crowded":
            _publish(root, row, event_queue)  # the closed episode's lines go out before a new one takes its row
            if not all(notice.get("recorded") or _held(root, notice["id"]) for notice in row["notices"].values()):
                log.warning("Max episode %s keeps its row: its lines are not yet held by the outbox", row.get("id"))
                return ""  # still owed; the next fresh start hands them over again
        with store.open_store(root, write=True) as txn:
            row, event = transition(txn.get_row(SET, IDENTITY), fact, task_id=task_id, now=utc_now_iso())
            if event:
                txn.put_row(SET, IDENTITY, row)
        if row and _publish(root, row, event_queue):
            with store.open_store(root, write=True) as txn:  # the current row keeps a concurrent start's facts
                current = txn.get_row(SET, IDENTITY)
                if current and current.get("id") == row["id"]:
                    for which, notice in current["notices"].items():
                        notice["recorded"] = bool(notice.get("recorded") or row["notices"].get(which, {}).get("recorded"))
                    if current.get("closed") and current["notices"]["close"]["recorded"]:
                        txn.delete_row(SET, IDENTITY)  # the remover: the close line is in Main
                    else:
                        txn.put_row(SET, IDENTITY, current)
        return event
    except Exception:
        log.warning("Max episode of task %s not observed", task_id, exc_info=True)
        return ""
