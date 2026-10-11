"""Observability blobs are never deleted and never counted (CPL4-C22, owner 7A; TZ-1 A).

The retention knob that deleted nothing is retired (absent from the module, listed in
``RETIRED_SETTING_KEYS`` so stored ghosts drop on settings load), and the startup census
that walked every manifest and blob to count them is gone with it: startup neither lists
nor touches the store.
"""

from __future__ import annotations

import os
import pathlib
import time


def _seed_store(tmp_path):
    calls = tmp_path / "observability" / "calls" / "t1"
    blobs = tmp_path / "observability" / "blobs"
    calls.mkdir(parents=True)
    blobs.mkdir(parents=True)
    aged = time.time() - 4000 * 86400
    paths = [calls / "a.json", calls / "b.json", blobs / "x.gz", blobs / "y.gz", blobs / "z.gz"]
    for path in paths:
        path.write_bytes(b"data")
        os.utime(path, (aged, aged))
    return paths


def test_startup_neither_counts_nor_deletes_observability_blobs(tmp_path, monkeypatch):
    import ouroboros.observability as observability
    from ouroboros import server_maintenance as sm

    monkeypatch.setattr(sm, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OUROBOROS_OBSERVABILITY_RETENTION_DAYS", "1")  # inert: retired
    paths = _seed_store(tmp_path)
    touched = []
    real_stat = os.stat

    def spy(path, *args, **kwargs):
        if "observability" in str(path):
            touched.append(str(path))
        return real_stat(path, *args, **kwargs)

    # The spy watches startup only: from Python 3.11 pathlib stats through ``os.stat``, so the
    # checks below would otherwise count themselves as startup touches. Python 3.10 pathlib
    # bound ``os.stat`` at import in its accessor, which a pathlib census would stat through.
    with monkeypatch.context() as startup:
        startup.setattr(os, "stat", spy)
        if hasattr(pathlib, "_NormalAccessor"):
            startup.setattr(pathlib._NormalAccessor, "stat", staticmethod(spy))
        sm._startup_prune_sweeps()
    assert touched == [], touched
    assert all(path.read_bytes() == b"data" for path in paths)
    assert not hasattr(observability, "prune_observability_blobs")


def test_retention_knob_is_retired_everywhere():
    import inspect

    import ouroboros.observability as observability
    from ouroboros.settings_defaults import RETIRED_SETTING_KEYS

    source = inspect.getsource(observability)
    # The docstring may still NAME the retired knob; nothing may READ it.
    assert 'environ.get("OUROBOROS_OBSERVABILITY_RETENTION_DAYS"' not in source
    assert "OUROBOROS_OBSERVABILITY_RETENTION_DAYS" in RETIRED_SETTING_KEYS
