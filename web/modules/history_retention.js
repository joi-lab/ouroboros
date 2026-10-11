// History placement is independent of task completion. Ordinary progress belongs
// in details and Logs; only a storage problem belongs on the collapsed card.
export function historyRetentionView(record) {
    const fact = record?.history_retention || (record?.type === 'history_retention' ? record : null);
    const status = fact?.status === 'deferred' ? 'pending' : fact?.status;
    if (!['pending', 'problem', 'complete'].includes(status)) return null;
    const problem = status === 'problem';
    const reasons = (Array.isArray(fact.problem_reasons) ? fact.problem_reasons : [])
        .filter((row) => row && typeof row.reason === 'string' && row.reason)
        .map((row) => `${Number.isInteger(row.count) && row.count > 1 ? `${row.count} × ` : ''}${row.reason}`).join('\n');
    return {
        status, phase: problem ? 'warn' : 'info',
        headline: problem ? 'Task history needs attention'
            : status === 'complete' ? 'Task history saved' : 'Saving task history',
        body: problem ? `Some task history could not be saved.\n${reasons || 'No failure reason was recorded.'}`
            : status === 'complete' ? 'Task history has been saved.'
                : 'Task history is being saved in the background.',
        problem: problem ? 'History storage problem' : '',
        meta: ['pending_count', 'problem_count', 'promoted_ref_count', 'promoted_source_handle_count']
            .filter((key) => Number.isInteger(fact[key]) && fact[key] >= 0)
            .map((key) => `${key}=${fact[key]}`),
    };
}

export function syncHistoryRetentionItem(record, detail) {
    const view = historyRetentionView(detail);
    if (!record || !view) return false;
    const key = `history-retention|${record.groupId}`;
    const current = record.items.find((item) => item.dedupeKey === key);
    if (current?.body === view.body && record.historyRetentionProblem === view.problem) return false;
    record.historyRetentionProblem = view.problem;
    const item = {
        phase: view.phase, headline: view.headline, fullHeadline: view.headline,
        body: view.body, fullBody: view.body, receipt: true, count: 1,
        ts: '', sourceTs: String(detail.ts || ''), dedupeKey: key,
        lineKey: `history-retention-${String(record.groupId).replace(/[^A-Za-z0-9_-]/g, '-')}`,
    };
    if (current) Object.assign(current, item);
    else record.items.push(item);
    return true;
}
