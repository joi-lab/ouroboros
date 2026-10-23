"""Attention facts on accepted room rows, and indexed place activity projections.

No event ledger: the existing dialogue dedup record owns the intake decision.
The task store continues to own every focus that actually started.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

from ouroboros import dialogue_store, store

_PLACE = "json_extract(body, '$.place_id')"
_ARRIVAL = "stream='chat' AND kind='place_arrival'"
_OPEN = "status NOT IN ('completed','failed','cancelled')"


class SourceProjection(dict):
    """A reader projection retains its original immutable row for source addressing."""
    def __init__(self, body, source):
        super().__init__(body, context_source_ref=source)
        self.source_body = body


def project_source(record):
    """Project a deferred source pointer without mutating the accepted source bytes."""
    body = dialogue_store.decode(record["body"])
    if body.get("type") != "place_arrival" or "dispatch_input" not in record.keys():
        return body
    extra = json.loads(record["dispatch_input"] or "{}")
    source = extra.get("place_context_source_ref") if isinstance(extra, dict) else None
    return SourceProjection(body, source) if isinstance(source, dict) else body


@contextmanager
def intake_lock(root, conversation_key):
    from ouroboros.presence_runner import _configured_gate
    gate = _configured_gate(Path(root))
    key, lock = gate._conversation("intake:" + conversation_key)
    with lock, gate._file_lock(gate._conversation_file(key)):
        yield


def _record(conn, task_id):
    return conn.execute("SELECT seq, body, dispatch_input FROM dialogue INDEXED BY dialogue_task "
                        "WHERE stream='chat' AND task_id=? AND direction='in' ORDER BY seq LIMIT 1",
                        (task_id,)).fetchone()


def heard_replay(root, task_id, identity="", continuation_version=0):
    from ouroboros.presence_runner import PresenceTurnError, PresenceTurnResult
    if not dialogue_store.present(root):
        return None
    with dialogue_store.transaction(root, write=False) as conn:
        row = _record(conn, task_id)
    if row is None:
        return None
    facts = json.loads(row['dispatch_input'] or '{}').get('place_attention', {})
    if facts.get('decision') != 'heard':
        return None
    body = dialogue_store.decode(row['body'])
    if identity and body.get('presence_event_identity') != identity:
        raise PresenceTurnError('presence_event_identity_conflict', 'source_event_id', turn_ref=task_id)
    return PresenceTurnResult(outcome='silent', text='', task_id=task_id,
                              delivery_reporting_version=facts.get('delivery_reporting_version', 0),
                              continuation_version=continuation_version)


def record_heard(root, task, event, reason, *, captured_task_id=None):
    from ouroboros.presence_runner import _stored_turn, presence_result_from_stored
    from dataclasses import replace
    identity = task['metadata']['presence_event_identity']
    arrival_id = captured_task_id or task['id']
    with intake_lock(root, event.conversation_key):
        # A certified retry has a new physical id, but custody still belongs to
        # that attempt while the immutable arrival keeps its original id.
        stored = _stored_turn(root, task['id'], identity)
        if stored:
            if stored.get('status') not in {'completed', 'failed'}:
                return None  # admitted custody is never overwritten by an attention decision
            return replace(presence_result_from_stored(stored, task['id']), continuation_version=event.continuation_version)
        with dialogue_store.transaction(root, write=True) as conn:
            row = _record(conn, arrival_id)
            if row is None or dialogue_store.decode(row['body']).get('presence_event_identity') != identity:
                raise ValueError('Place attention requires its captured event')
            facts = json.loads(row['dispatch_input'] or '{}')
            facts.setdefault('place_attention', {'decision': 'heard', 'reason': reason,
                             'delivery_reporting_version': event.delivery_reporting_version})
            conn.execute("UPDATE dialogue SET dispatch_input=? WHERE stream='chat' AND seq=?",
                         (json.dumps(facts), row['seq']))
        return heard_replay(root, arrival_id, identity, event.continuation_version)


def retain_focus_source(root, task_id):
    """Only an admitted focus needs the immutable context-source blob.

    Its pointer lives in the existing dedup record, so source row bytes and byte
    coordinates stay unchanged. dialogue_store projects it for every row reader.
    Caller holds intake_lock through this operation and the running-task write.
    """
    from ouroboros import chat_chain
    with dialogue_store.transaction(root, write=False) as conn:
        row = _record(conn, task_id)
    if row is None:
        raise ValueError('Focus requires its captured event')
    body = dialogue_store.decode(row['body'])
    facts = json.loads(row['dispatch_input'] or '{}')
    if body.get('context_source_ref') or facts.get('place_context_source_ref'):
        return
    source = {'recorded_at': body['ts'], 'source_event_id': body['client_message_id'],
              'text': body['text'], 'transport': body['transport']}
    ref = chat_chain.retain_memory_source(SimpleNamespace(drive_root=root, task_id=task_id), 'presence-event',
        json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode(), 'json')
    with dialogue_store.transaction(root, write=True) as conn:
        conn.execute("UPDATE dialogue SET dispatch_input=json_set(COALESCE(dispatch_input,'{}'), "
                     "'$.place_context_source_ref',json(?)) WHERE stream='chat' AND seq=?",
                     (json.dumps(ref), row['seq']))


def _dialogue_indexes(root):
    if not dialogue_store.present(root):
        return False
    with dialogue_store.transaction(root, write=False) as conn:
        found = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                          "AND name IN ('dialogue_place_rooms','dialogue_place_recent')")}
    if len(found) != 2:
        # One-time index build, including pre-upgrade captured arrivals. No body
        # backfill and no second ledger; later reads only seek these B-trees.
        with dialogue_store.transaction(root, write=True) as conn:
            conn.execute(f"CREATE INDEX IF NOT EXISTS dialogue_place_rooms ON dialogue({_PLACE},chat_id,seq DESC) WHERE {_ARRIVAL}")
            conn.execute(f"CREATE INDEX IF NOT EXISTS dialogue_place_recent ON dialogue({_PLACE},ts DESC) WHERE {_ARRIVAL}")
    return True


def _open_counts(root, place):
    """One aggregate over indexed nonterminal focuses, never task-document loads."""
    if not (Path(root) / store.STORE_REL).exists() or store.migrating(root):
        return None
    with store.open_store(root, write=False) as txn:
        exists = txn.conn.execute("SELECT 1 FROM sqlite_master WHERE name='tasks_place_open'").fetchone()
    if not exists:
        with store.open_store(root, write=True) as txn:
            txn.conn.execute(f"CREATE INDEX IF NOT EXISTS tasks_place_open ON tasks(presence_binding_id,task_id) WHERE {_OPEN}")
    bindings = tuple(dict.fromkeys((place.place_id, *place.legacy_binding_ids)))
    with store.open_store(root, write=False) as txn:
        rows = txn.conn.execute(
            "SELECT json_extract(b.body,'$.chat_id') AS room,COUNT(*) AS count FROM tasks t INDEXED BY tasks_place_open "
            "JOIN task_bodies b ON b.task_id=t.task_id WHERE " + _OPEN +
            " AND t.presence_binding_id IN (" + ','.join('?' for _ in bindings) + ") "
            "AND json_extract(b.body,'$.metadata.wake_cause')='focus' "
            "AND COALESCE(t.parent_task_id,'')='' GROUP BY room", bindings).fetchall()
    return {int(r['room']): int(r['count']) for r in rows if r['room'] is not None}


def room_index(root, place):
    if not _dialogue_indexes(root):
        return []
    counts = _open_counts(root, place)
    rooms = []
    # A loose index scan: one seek per captured room, independent of arrival
    # history size. A busy room with a million heard rows still takes one seek.
    with dialogue_store.transaction(root, write=False) as conn:
        last = None
        while True:
            bound = '' if last is None else ' AND chat_id>?'
            args = (place.place_id,) if last is None else (place.place_id, last)
            row = conn.execute(f"SELECT chat_id FROM dialogue INDEXED BY dialogue_place_rooms WHERE {_ARRIVAL} "
                               f"AND {_PLACE}=? AND chat_id IS NOT NULL{bound} ORDER BY chat_id LIMIT 1", args).fetchone()
            if row is None:
                break
            last = int(row[0])
            rooms.append({'room_chat_id': last, 'unanswered_count': None if counts is None else counts.get(last, 0),
                          'address': f'chat:{last}'})
    return rooms


def activity(root, place):
    if not _dialogue_indexes(root):
        return {'unanswered': 0, 'last_activity_at': ''}
    counts = _open_counts(root, place)
    # O(log arrivals + open focuses), not O(all historical arrivals). The
    # newest row is an index-only LIMIT 1; counts decode no task documents.
    with dialogue_store.transaction(root, write=False) as conn:
        row = conn.execute(f"SELECT ts FROM dialogue INDEXED BY dialogue_place_recent WHERE {_ARRIVAL} "
                           f"AND {_PLACE}=? ORDER BY ts DESC LIMIT 1", (place.place_id,)).fetchone()
    return {'unanswered': None if counts is None else sum(counts.values()),
            'last_activity_at': row[0] if row else ''}


def account_candidates(root, place_id):
    """At most 20 people from the newest 50 arrivals: one index seek + 50 rows.

    This is a recent UI suggestion, never owner identity evidence or a history
    scan. It uses the existing place/time index and stores no second actor list.
    """
    if not _dialogue_indexes(root):
        return []
    with dialogue_store.transaction(root, write=False) as conn:
        rows = conn.execute(f"SELECT json_extract(body,'$.sender_session_id') AS actor, "
                            "json_extract(body,'$.sender_label') AS label, "
                            "json_extract(body,'$.transport.actor.is_bot') AS bot "
                            f"FROM dialogue INDEXED BY dialogue_place_recent WHERE {_ARRIVAL} "
                            f"AND {_PLACE}=? ORDER BY ts DESC LIMIT 50", (place_id,)).fetchall()
    people = {}
    for row in rows:
        actor = str(row['actor'] or '')
        if actor and not row['bot'] and actor not in people:
            people[actor] = {'platform_actor_id': actor, 'label': str(row['label'] or actor)}
    return list(people.values())[:20]
