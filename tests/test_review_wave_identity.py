"""A collected host review keeps one identity across durable publications."""
import copy
import json
import threading
import time
from types import SimpleNamespace




def test_one_operation_through_pending_and_settled_publications(tmp_path, monkeypatch):
    from ouroboros import review_custody, review_dispatch, review_projection
    from ouroboros.loop_acceptance_review import _record_host_acceptance_run, acceptance_run_pending
    from ouroboros.review_records import ReviewRequest, ReviewSlot
    from ouroboros.review_substrate import run_review_request
    from ouroboros.task_results import load_task_result, merge_review_projection
    from tests.test_loop_acceptance_gate import _seed_acceptance_root

    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    calls, stamps = [], []
    real_settle = review_custody._settle_review_attempt

    def settle(*args, **kwargs):
        try:
            return real_settle(*args, **kwargs)
        finally:
            settled.set()

    monkeypatch.setattr(review_custody, '_settle_review_attempt', settle)

    class HeldModel:
        def chat(self, **kwargs):
            # A recording LLM bypasses the real API accounting boundary, so
            # explicitly invoke its already-bound dispatch stamp there.
            review_dispatch.invoke_bound_api_review_paid_stamp()
            calls.append(kwargs)
            entered.set()
            assert release.wait(10)
            return {'content': json.dumps({'verdict': 'FAIL', 'summary': 'Actual retained criticism',
                                         'findings': []})}, {'prompt_tokens': 5, 'completion_tokens': 2}

    ctx = SimpleNamespace(task_id='panel-audit', task_attempt=1, drive_root=tmp_path,
        budget_drive_root=tmp_path, task_metadata={}, pending_events=[], event_queue=None)
    _seed_acceptance_root(tmp_path, ctx.task_id, ctx)
    ctx._review_paid_stamp = review_dispatch.ReviewPaidStamp(lambda: stamps.append('physical'))
    request = ReviewRequest(surface='task_acceptance', task_id=ctx.task_id, task_attempt=1,
        goal='Audit publication identity', subject='The same complete answer',
        evidence={'requirement': 'same'}, retry_key='panel-audit:paid-one', drain_deadline=time.monotonic())
    slot = ReviewSlot(slot_id='one', model='model/original', effort='high', timeout_sec=20)
    trace = {'review_runs': []}
    snapshots = []
    try:
        first = run_review_request(request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx, llm=HeldModel())
        assert entered.wait(5) and acceptance_run_pending(first)
        binding = review_projection.build_review_binding(candidate=request.subject, evidence=request.evidence,
                                                         fence_token_or_state='same fence')
        binding['paid_identity'] = 'paid-one'
        owner = SimpleNamespace(tools=SimpleNamespace(_ctx=ctx), llm_trace=trace, review_binding=binding)
        run = _record_host_acceptance_run(owner, first)

        def publish():
            review_projection.publish_acceptance_checkpoint(ctx, trace)
            saved = load_task_result(tmp_path, ctx.task_id)
            snapshots.append(copy.deepcopy(saved['review_projection']))
            return saved

        publish()
        advanced = review_dispatch.reconcile_pending_acceptance_runs(trace, drive_root=tmp_path, usage_ctx=ctx)
        assert advanced == 0 and acceptance_run_pending(run)
        publish()
        release.set()
        assert settled.wait(5)
        advanced = review_dispatch.reconcile_pending_acceptance_runs(trace, drive_root=tmp_path, usage_ctx=ctx)
        assert advanced == 1 and not acceptance_run_pending(run)
        saved = publish()
        assert len(calls) == len(stamps) == 1
        assert len(trace['review_runs']) == 1
        rows = saved['review_projection']['panels']
        assert len(rows) == 1
        assert [len(p["panels"]) for p in snapshots] == [1, 1, 1]
        operations = {actor['operation_id'] for panel in rows for actor in panel['actors']}
        assert len(operations) == 1 and run['actors'][0]['operation_id'] in operations
        assert run['actors'][0]['parsed']['verdict'] == 'FAIL'
        assert rows[-1]['publication_revision'] == 3
        assert rows[0]['panel_id'] == binding['panel_id']
        # Neither another logical record at the same binding (host error,
        # rebuttal/new wave) nor another task attempt is collapsed.
        second = {**rows[0], 'panel_index': 1, 'publication_revision': 4}
        next_attempt = {**rows[0], 'task_attempt': 2, 'publication_revision': 1}
        merged = merge_review_projection(saved['review_projection'], {'panels': [second, next_attempt]})
        assert len(merged['panels']) == 3
    finally:
        release.set()
        assert settled.wait(5)
