from __future__ import annotations

import threading

from ouroboros.startup_historical_audit import HistoricalAudit, _report_fields


def test_historical_audit_report_is_bounded_and_unknown_on_missing_fields():
    valid = _report_fields(
        b'{"status":"completed","facts_written":2,"manifests_checked":9,"wall_seconds":1.5,"cpu_seconds":0.25}'
    )
    assert valid["status"] == "completed"
    assert valid["manifests_checked"] == 9
    assert _report_fields(b'{"status":"completed","facts_written":2}') == {}
    assert _report_fields(b'{"status":"completed","facts_written":2,"manifests_checked":9,"wall_seconds":NaN,"cpu_seconds":0}') == {}
    assert _report_fields(b'{"status":"completed","facts_written":2,"manifests_checked":9,"wall_seconds":1,"cpu_seconds":0,"path":"secret"}') ["status"] == "completed"


def test_historical_audit_stop_before_start_latches_without_spawning(tmp_path):
    audit = HistoricalAudit()
    audit.stop()
    audit.start(tmp_path, tmp_path)
    assert audit._launched is False
    assert audit._process is None


def test_historical_audit_stop_signals_published_child_without_waiting():
    audit = HistoricalAudit()
    killed = threading.Event()

    class Child:
        def kill(self):
            killed.set()

    audit._process = Child()
    audit.stop()
    assert killed.is_set()
