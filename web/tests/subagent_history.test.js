import test from 'node:test';
import assert from 'node:assert/strict';
import { rowMeta } from '../modules/subagent_status_primitives.js';

test('missing requested options are not a proved change of settings; known empty remains comparable', () => {
    const row = { subagent_id: 'worker', route: { kind: 'agent_session', target_id: 'codex=model' },
        access: 'full', effort: '', processing_preference: '' };
    const receipt = { selected_subagent_id: 'worker', route: 'codex', requested_model: 'model',
        applied_model: 'served-model', applied_profile: 'observed-account',
        identity: { kind: 'agent_session', target_id: 'codex=model', credential_profile_id: '', access: 'full', effort: '' } };
    const state = { snapshot: { subagent_last_delegation: receipt } };
    const unknown = rowMeta(row, state, []).text;
    assert.match(unknown, /settings not fully reported/);
    assert.doesNotMatch(unknown, /Earlier settings/);
    assert.match(unknown, /account observed-account/);
    receipt.identity.processing_preference = '';
    assert.match(rowMeta(row, state, []).text, /Last run:/);
    assert.match(rowMeta({ ...row, effort: 'high' }, state, []).text, /Earlier settings:/);
});
