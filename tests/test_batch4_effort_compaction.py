"""#1414 effort evidence through Batch4 ledger compaction, row memo and binding index.

Upstream added ``effort``/``effort_resolution`` to attempt rows; Batch4 added the
compaction binding carriage and indexed money reads. A folded attempt keeps its
evidence in the verbatim archive, a retained chain is copied whole, and a late
receipt after compaction binds only its own attempt's candidate effort.
"""
import json

from ouroboros import _usage_rows_memo as memo
from ouroboros import usage_accounting as ua
from ouroboros import usage_compaction as uc
from ouroboros.usage_admission import ledger_billing_binding
from tests import fixtures_usage_compaction as _fixtures
from tests.fixtures_usage_compaction import _compact, _ledger_rows, _request

data_root = _fixtures.data_root
data_root_any_tier = _fixtures.data_root_any_tier

RESOLUTION = {"requested": "xhigh", "submitted": "high", "parameter": "reasoningEffort", "observed": None,
              "observedSource": None, "resolution": "downward", "source": "adapter"}


def _effort(requested, sent):
    return {"requested": requested, "sent": {"reasoning_effort": sent}, "sent_state": "explicit",
            "sent_source": "host_candidate", "reported": None, "report_source": None}


def _without_sequence(rows):
    return [{key: value for key, value in row.items() if key not in ("seq", "pre_compaction_seq")} for row in rows]


def test_effort_evidence_survives_compaction_bound_to_its_own_attempt(data_root):
    folded_effort, open_effort = _effort("ultra", "max"), _effort("low", "low")
    reported = {**folded_effort, "reported": "max", "report_source": "provider_applied_options"}
    folded = ua.reserve_attempt(_request(data_root, root_limit_usd=50.0, effort=folded_effort))
    ua.mark_dispatched(folded)
    ua.settle_attempt(folded, {"prompt_tokens": 10, "completion_tokens": 5, "effort": reported,
                               "effort_resolution": RESOLUTION}, cost_usd=0.1, cost_final=True)
    inflight = ua.reserve_attempt(_request(data_root, task_id="open", root_limit_usd=50.0, effort=open_effort))
    ua.mark_dispatched(inflight)
    ua.mark_unresolved(inflight, "provider went dark")
    binding = ledger_billing_binding(data_root, "root")
    assert binding and binding["billing_group_limit_usd"] == 50.0
    kept_before = [row for row in _ledger_rows(data_root) if row["attempt_id"] == inflight.attempt_id]

    receipt = _compact(data_root)

    assert receipt is not None and receipt["folded_attempt_count"] >= 1
    rows = _ledger_rows(data_root)
    assert folded.attempt_id not in {row["attempt_id"] for row in rows}
    assert uc.usage_attempt_recorded(data_root, folded.attempt_id)
    archived = [json.loads(line) for line in (data_root / receipt["archive_rel"]).read_text().splitlines()
                if line.strip()]
    final = [row for row in archived if row["attempt_id"] == folded.attempt_id][-1]
    assert (final["state"], final["effort"], final["effort_resolution"]) == ("settled", reported, RESOLUTION)
    kept = [row for row in rows if row["attempt_id"] == inflight.attempt_id]
    assert _without_sequence(kept) == _without_sequence(kept_before), "a retained chain is copied whole"
    for cold in (False, True):
        if cold:
            with memo._LEDGER_READ_CACHE_LOCK:
                memo._LEDGER_READ_CACHE.clear()
        finals = {row["attempt_id"]: row for row in ua.read_usage_records(data_root, final_only=True)}
        assert finals[inflight.attempt_id]["effort"] == open_effort
        assert "effort_resolution" not in finals[inflight.attempt_id]
    assert ledger_billing_binding(data_root, "root") == binding

    ua.settle_attempt(inflight, {"prompt_tokens": 3, "completion_tokens": 1, "effort_resolution": RESOLUTION},
                      cost_usd=0.25, cost_final=True)
    last = _ledger_rows(data_root)[-1]
    assert (last["attempt_id"], last["state"], last["settle_reason"]) == (inflight.attempt_id, "settled", "late_receipt")
    assert last["effort"] == open_effort and last["effort_resolution"] == RESOLUTION
    assert ledger_billing_binding(data_root, "root") == binding
