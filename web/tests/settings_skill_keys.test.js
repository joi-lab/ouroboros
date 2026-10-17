import test from 'node:test';
import assert from 'node:assert/strict';

import { settingsSaveEffectText } from '../modules/settings.js';

const RULE = 'skills pick up granted keys on their next read';

// #1122 (owner answer A): a running task keeps its start snapshot, while a skill reads
// the keys it is granted on its next read. The gateway's next-task bucket carries that
// rule once, whichever keys the save edited; the page never guesses a key's reader.

test('the next-task sentence states the skill-key rule once', () => {
    const text = settingsSaveEffectText({ next_task_changed: true });
    assert.equal(text, `Settings saved. Changes take effect on the next task; ${RULE}`);
    const mixed = settingsSaveEffectText({ immediate_changed: true, next_task_changed: true });
    assert.equal(mixed, `Settings saved. Some changes took effect immediately, others apply on the next task; ${RULE}`);
    assert.equal(mixed.split(RULE).length - 1, 1);
});

test('immediate, restart, no-change and unclassified saves keep their wording', () => {
    assert.equal(settingsSaveEffectText({ immediate_changed: true }), 'Settings saved. Changes took effect immediately');
    assert.equal(settingsSaveEffectText({ no_changes: true, next_task_changed: true }), 'No changes detected');
    assert.equal(settingsSaveEffectText({ restart_required: true, next_task_changed: true }),
        'Settings saved. Some changes require a restart to take effect');
    assert.equal(settingsSaveEffectText({ restart_state: { summary: 'x' }, next_task_changed: true }), 'Settings saved');
    assert.equal(settingsSaveEffectText({}), 'Settings saved');
});
