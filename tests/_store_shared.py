"""Store-era seams for what used to be ``task_results/<id>.json`` file mechanics in tests.

A test that wrote a result file to plant a row, read one back to inspect what production
wrote, or checked its presence, does the same thing against the current-facts store here.
Plain callables (many callers need them at module import time), never fixtures.
"""
from __future__ import annotations

import pathlib
from typing import Any, Dict, Optional, Tuple

from ouroboros.task_result_schema import SCHEMA_VERSION_KEY, TASK_RESULT_SCHEMA_VERSION
from ouroboros.task_store import delete_document, exists, patch_document, read_document, signature


def seed_result(root: Any, task_id: str, document: Dict[str, Any]) -> Dict[str, Any]:
    """Plant DOCUMENT as task TASK_ID's current facts, bypassing the lifecycle writer's reducer
    exactly as writing its file used to (the row is whatever the test says it is). The store
    holds admitted rows only, so an unstamped document is stamped; a test about LEGACY
    admission (quarantine, the pre-7 latch) writes the file and lets the import judge it."""
    body = dict(document)
    body.setdefault("task_id", str(task_id))
    body.setdefault(SCHEMA_VERSION_KEY, TASK_RESULT_SCHEMA_VERSION)
    return patch_document(root, task_id, lambda _current: body)


def read_result(root: Any, task_id: str) -> Optional[Dict[str, Any]]:
    """The stored document as written (no reader normalisation), ``None`` when absent."""
    return read_document(root, task_id)


def result_exists(root: Any, task_id: str) -> bool:
    return exists(root, task_id)


def result_signature(root: Any, task_id: str) -> Optional[Tuple[int, str]]:
    """``(write_seq, updated_at)``: what a test compares before and after an operation that
    must not write the row (the file era compared the file's bytes)."""
    return signature(root, task_id)


def delete_result(root: Any, task_id: str) -> bool:
    """Remove one task's row; ``True`` when it was there."""
    return delete_document(root, task_id)


def unreadable_result(monkeypatch, task_id: str, *, root=None) -> dict:
    """Make every body read of TASK_ID fail as the store does when it cannot answer: the
    store-era shape of the torn result file tests used to plant (``StoreError`` out of the
    row read, inside readers and inside the write transaction alike). ``root`` limits the
    failure to one drive's store; the returned switch (``{"on": True}``) lets a test make the
    row readable again ("after access recovers") without undoing its other patches."""
    from ouroboros import store

    real_body, real_row = store.Txn.body_text, store.Txn.row
    switch = {"on": True}
    wanted_root = None if root is None else pathlib.Path(root).resolve()

    def broken(self, tid):
        if not switch["on"] or str(tid) != str(task_id):
            return False
        return wanted_root is None or (self.root is not None and self.root.resolve() == wanted_root)

    def body_text(self, tid):
        if broken(self, tid):
            raise store.StoreError(f"simulated unreadable row: {tid}")
        return real_body(self, tid)

    def row(self, tid, *columns):
        if broken(self, tid):
            raise store.StoreError(f"simulated unreadable row: {tid}")
        return real_row(self, tid, *columns)

    from ouroboros import task_store

    real_views = task_store.display_documents

    def display_documents(root, task_ids, versions=None):  # a batch holding the row fails like a single read of it
        ids = [str(tid) for tid in task_ids]
        if switch["on"] and str(task_id) in ids and (
                wanted_root is None or pathlib.Path(root).resolve() == wanted_root):
            raise store.StoreError(f"simulated unreadable row: {task_id}")
        return real_views(root, ids, versions)

    monkeypatch.setattr(store.Txn, "body_text", body_text)
    monkeypatch.setattr(store.Txn, "row", row)
    monkeypatch.setattr(task_store, "display_documents", display_documents)
    return switch


def unavailable_store(monkeypatch) -> None:
    """Every store transaction fails with ``StoreBusy``: the whole store cannot answer."""
    from ouroboros import store, task_store

    def busy(*_args, **_kwargs):
        raise store.StoreBusy("simulated: current store unavailable")

    monkeypatch.setattr(store, "open_store", busy)
    monkeypatch.setattr(task_store, "open_store", busy)
