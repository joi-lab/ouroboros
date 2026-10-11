// The update letter: Ouroboros's own short paragraph about the pending
// official update, delivered inside the ordinary status payload and KEPT
// after the update lands.
//
// Two kinds of assertion live here, for two different failure modes. The
// projector cases pin the pure function (what the panel decides to say);
// the source pins guard the facts a pure test cannot see — where the section
// sits in the card, that the markdown pipeline is the sanitizing one, that
// its disposer runs before every re-render, and that none of this leaked
// into the apply flow; description refresh has its own compact control.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { updateLetterView, updateVerdict } from '../modules/updates.js';

const SOURCE = readFileSync(new URL('../modules/updates.js', import.meta.url), 'utf8')
    .replace(/\r\n?/g, '\n');

const CURRENT = {
    managed: true,
    check_ok: true,
    available: false,
    current_version: '6.114.0',
    current_short_sha: 'abcd1234',
    current_sha: 'b'.repeat(40), running_sha: 'b'.repeat(40),
    checked_target_sha: 'b'.repeat(40),
};
const AVAILABLE = {
    managed: true,
    check_ok: true,
    available: true,
    safe_to_apply: true,
    current_version: '6.113.5',
    current_short_sha: 'abcd1234',
    latest_version: '6.114.0',
    latest_short_sha: 'ef567890',
    current_sha: 'a'.repeat(40), running_sha: 'a'.repeat(40),
    latest_sha: 'b'.repeat(40),
};

function letter(overrides = {}) {
    return {
        state: 'ready',
        relation: 'pending',
        description_current: true,
        text: 'This update makes the Updates panel explain itself.',
        author_version: '6.113.5',
        target_version: '6.114.0',
        written_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString(),
        error_kind: '',
        error_text: '',
        key: { base_sha: 'a'.repeat(40), target_sha: 'b'.repeat(40), update_channel: 'stable', target_ref: 'managed/main' },
        has_last_good: false,
        ...overrides,
    };
}


test('a payload without a letter leaves the section hidden', () => {
    for (const data of [AVAILABLE, { ...AVAILABLE, letter: null }, { ...AVAILABLE, letter: 'nope' }]) {
        const view = updateLetterView(data, '');
        assert.equal(view.state, 'none');
        assert.equal(view.markdown, '');
        assert.equal(view.label, '');
        assert.equal(view.note, '');
        assert.equal(view.failure, null);
    }
    assert.equal(updateLetterView().state, 'none');
});


test('a pending letter is "What\'s new" with its provenance and no note', () => {
    const view = updateLetterView({ ...AVAILABLE, letter: letter() }, '');
    assert.equal(view.state, 'ready');
    assert.equal(view.relation, 'pending');
    assert.equal(view.label, "What's new");
    assert.match(view.markdown, /explain itself/);
    assert.equal(view.note, '');
    assert.equal(view.failure, null);
    assert.equal(view.meta.authorVersion, '6.113.5');
    assert.equal(view.meta.targetVersion, '6.114.0');
    // Same four buckets as the action row's "checked N ago" — one vocabulary.
    assert.equal(view.meta.ageText, '3 h ago');
    assert.equal(
        updateVerdict({ ...AVAILABLE, letter: letter() }, '').checkedAgo,
        '',
        'the letter age is not the check age',
    );
});


test('an applied letter about an older version than the running one says so', () => {
    // The kept letter describes a version this one includes but has moved past.
    const view = updateLetterView({ ...CURRENT, current_version: '6.115.0', letter: letter({ relation: 'applied' }) }, '');
    assert.equal(view.label, 'What changed in this version');
    assert.equal(view.note, '');
    assert.equal(view.meta.targetVersion, '6.114.0', 'provenance remains available in Details');
});


test('an applied letter is relabelled, never deleted', () => {
    const view = updateLetterView({ ...CURRENT, letter: letter({ relation: 'applied' }) }, '');
    assert.equal(view.state, 'ready');
    assert.equal(view.label, 'What changed in this version');
    assert.equal(view.note, '', 'the running version IS the target: nothing to disclaim');
    assert.match(view.markdown, /explain itself/);
});


test('a superseded letter keeps its text and says which range it was written for', () => {
    const view = updateLetterView({
        ...AVAILABLE,
        latest_version: '6.115.0', latest_sha: 'c'.repeat(40),
        letter: letter({ relation: 'superseded', description_current: false }),
    }, '');
    assert.equal(view.state, 'ready');
    assert.equal(view.label, "What's new");
    assert.equal(view.note, 'Description needs refreshing.');
    assert.match(view.markdown, /explain itself/);
});


test('a letter whose HEAD moved elsewhere is marked, and an unnamed relation lands there too', () => {
    const moved = updateLetterView({ ...CURRENT, letter: letter({ relation: 'other', description_current: false }) }, '');
    assert.equal(moved.relation, 'other');
    assert.equal(moved.label, "What's new");
    assert.equal(moved.note, 'This description was written for an earlier update.');

    // A relation this client does not know is treated as the honest "other":
    // keep the text, mark it — never claim it describes the update on offer.
    const unnamed = updateLetterView({ ...CURRENT, letter: letter({ relation: 'sideways', description_current: false }) }, '');
    assert.equal(unnamed.relation, 'other');
    assert.equal(unnamed.note, 'This description was written for an earlier update.');

    // Versionless provenance degrades instead of printing "undefined".
    const bare = updateLetterView({
        ...CURRENT,
        letter: letter({ relation: 'other', author_version: '', target_version: '', description_current: false }),
    }, '');
    assert.equal(bare.note, 'This description was written for an earlier update.');
});


test('a failed letter with a last good text shows the text plus the failure reason', () => {
    const view = updateLetterView({
        ...AVAILABLE,
        latest_sha: 'c'.repeat(40),
        letter: letter({
            state: 'failed',
            description_current: false,
            error_kind: 'provider_unavailable',
            error_text: 'openrouter 503',
            has_last_good: true,
        }),
    }, '');
    assert.equal(view.state, 'failed');
    assert.match(view.markdown, /explain itself/, 'the last good letter survives the failed rewrite');
    assert.deepEqual(view.failure, { kind: 'provider_unavailable', text: 'openrouter 503', failedAt: '', key: null });
    assert.equal(view.note, 'Refresh failed. Previous description kept.');
    assert.equal(view.failure.text, 'openrouter 503', 'full cause stays in Details');
});


test('a kept letter about an earlier target is labelled by its own range, with the failure beside it', () => {
    // The backend relates a kept letter by ITS range (update_letter.py::project_letter),
    // so a letter about 6.114.0 kept through a failed rewrite for 6.115.0 arrives as
    // superseded: the card offering 6.115.0 must not present it as that update's letter.
    const view = updateLetterView({
        ...AVAILABLE,
        latest_version: '6.115.0', latest_sha: 'c'.repeat(40),
        letter: letter({
            state: 'failed', relation: 'superseded', description_current: false,
            error_kind: 'provider_unavailable', error_text: '503', has_last_good: true,
        }),
    }, '');
    assert.equal(view.state, 'failed');
    assert.equal(view.label, "What's new");
    assert.equal(view.meta.targetVersion, '6.114.0');
    assert.equal(
        view.note,
        'Refresh failed. Previous description kept.',
    );
});


test('a failed letter with no text still names why there is nothing to read', () => {
    const view = updateLetterView({
        ...AVAILABLE,
        letter: letter({ state: 'failed', text: '', description_current: false, error_kind: 'no_credentials', error_text: '', has_last_good: false }),
    }, '');
    assert.equal(view.state, 'failed');
    assert.equal(view.markdown, '');
    assert.deepEqual(view.failure, { kind: 'no_credentials', text: '', failedAt: '', key: null });
    assert.equal(view.note, 'Refresh failed. No description is available yet.');
});


test('an empty or unnamed letter state renders nothing rather than an empty block', () => {
    assert.equal(updateLetterView({ ...AVAILABLE, letter: letter({ text: '   ' }) }, '').state, 'none');
    assert.equal(updateLetterView({ ...AVAILABLE, letter: letter({ state: 'writing' }) }, '').state, 'none');
});


test('the letter hides wherever it could only mislead', () => {
    // Verdict states with no trustworthy update story to attach a letter to.
    const hiddenByVerdict = [
        ['unmanaged', { managed: false, letter: letter() }],
        ['unknown', { managed: true, warnings: ['status_error:boom'], check_ok: null, available: false, letter: letter() }],
        ['unchecked', {
            managed: true, check_ok: null, available: false,
            warnings: ['official_status_requires_check'], letter: letter(),
        }],
    ];
    for (const [expected, data] of hiddenByVerdict) {
        assert.equal(updateVerdict(data, '').state, expected, `fixture no longer produces ${expected}`);
        assert.equal(updateLetterView(data, '').state, 'none', `${expected} must not carry a letter`);
    }
    // The restart phase: the served-SHA reload owns the card.
    assert.equal(updateLetterView({ ...AVAILABLE, letter: letter() }, 'restarting').state, 'none');
    // …and the phases that keep it: a passive refresh (tab reopen) or a running
    // check must not blank the last known paragraph, and an owner mid-update is
    // exactly who wants to read what the update brings.
    for (const phase of ['', 'loading', 'checking', 'preflighting', 'updating', 'restart_required', 'restart_needed']) {
        assert.equal(updateLetterView({ ...AVAILABLE, letter: letter() }, phase).state, 'ready', phase);
    }
});


test('a letter-bearing payload leaves the verdict byte-for-byte identical', () => {
    for (const base of [CURRENT, AVAILABLE, { managed: true, update_tx: { active: true, phase: 'rolling_back' } }]) {
        for (const phase of ['', 'checking', 'updating', 'restart_required']) {
            const without = updateVerdict(base, phase);
            const withLetter = updateVerdict({ ...base, letter: letter() }, phase);
            assert.equal(withLetter.state, without.state);
            assert.equal(withLetter.headline, without.headline);
            assert.equal(withLetter.tone, without.tone);
            assert.deepEqual(withLetter.action, without.action);
            assert.deepEqual(withLetter.chips, without.chips);
            assert.deepEqual(withLetter.warnings, without.warnings);
            assert.equal(withLetter.hint, without.hint);
        }
    }
});

test('currentness comes from the server range fact, never VERSION or mutable checkout', () => {
    const data = { ...AVAILABLE, current_sha: 'x'.repeat(40), letter: letter() };
    assert.equal(updateLetterView(data).descriptionCurrent, true, 'disk movement is not adoption');
    const baseChanged = updateLetterView({ ...data, running_sha: 'c'.repeat(40),
        letter: letter({ description_current: false }) });
    assert.equal(baseChanged.descriptionCurrent, false);
    assert.equal(baseChanged.note, 'Description needs refreshing.');
    assert.equal(updateLetterView({ ...data, letter: letter({ description_current: undefined }) }).descriptionCurrent, false);
});

test('applied old text keeps a current failure distinct from historical failure', () => {
    const failed = letter({ state: 'failed', relation: 'applied', has_last_good: true, description_current: false,
        failed_at: '2026-10-03T13:07:00Z', error_text: 'Capacity unavailable',
        latest_failed_key: { base_sha: 'b'.repeat(40), target_sha: 'c'.repeat(40) } });
    const status = { ...AVAILABLE, running_sha: 'b'.repeat(40), latest_sha: 'c'.repeat(40), letter: failed };
    const current = updateLetterView(status);
    assert.match(current.note, /^Refresh failed on/);
    assert.equal(current.failure.key.base_sha, 'b'.repeat(40));
    assert.equal(current.meta.writtenAt, failed.written_at);
    assert.equal(current.markdown, failed.text);
    const adopted = updateLetterView({ ...status, available: false, running_sha: 'c'.repeat(40) });
    assert.equal(adopted.failure, null);
    assert.equal(adopted.note, '');
    assert.equal(adopted.markdown, failed.text);
});

test('failed check preserves the visible prior description', () => {
    const view = updateLetterView({ ...AVAILABLE, check_ok: false,
        warnings: ['fetch_error:offline'], letter: letter() });
    assert.equal(view.markdown, letter().text);
});

test('a reusable successful same-range description retires the old failure headline', () => {
    const view = updateLetterView({ ...AVAILABLE, letter: letter({ state: 'failed', has_last_good: true,
        latest_failed_key: { base_sha: 'a'.repeat(40), target_sha: 'b'.repeat(40) },
        error_text: 'Previous attempt failed', failed_at: '2026-10-02T05:25:29Z' }) });
    assert.equal(view.descriptionCurrent, true);
    assert.equal(view.failure, null);
    assert.equal(view.note, '');
});


// --- Source pins: the DOM contract a pure projector cannot see --------------

test('the letter section keeps one compact refresh outside its authored body', () => {
    const actionRow = SOURCE.indexOf('class="settings-action-row updates-action-row"');
    const section = SOURCE.indexOf('<section class="updates-letter" id="updates-letter" aria-labelledby="updates-letter-label" hidden>');
    const recovery = SOURCE.indexOf('<details class="updates-recovery">');
    assert.ok(actionRow > -1 && section > -1 && recovery > -1, 'the card template moved');
    assert.ok(actionRow < section, 'the letter belongs BELOW the single primary action');
    assert.ok(section < recovery, 'the letter belongs ABOVE the Recovery disclosure');

    const card = SOURCE.slice(section, recovery);
    assert.match(card, /class="updates-letter-head"/);
    // A real heading names the section (aria-labelledby above), like Recovery's h4.
    assert.match(card, /<h4 class="updates-letter-label" id="updates-letter-label">/);
    assert.match(card, /class="updates-letter-meta"/);
    assert.match(card, /class="updates-letter-note"/);
    assert.match(card, /class="updates-letter-body ui-rich-content" id="updates-letter-body">/);
    // The enhancer marks the body itself; a static or duplicate mark would claim
    // an un-enhanced node is enhanced.
    assert.doesNotMatch(card, /data-chat-markdown-enhanced/);
    assert.doesNotMatch(SOURCE, /chatMarkdownEnhanced/);
    assert.match(card, /class="btn btn-ghost btn-sm updates-letter-refresh"/);
    assert.match(card, /aria-label="Refresh description"/);
    assert.equal((card.match(/<button/g) || []).length, 1);
    assert.match(card, /<details class="updates-letter-details"/);
});


test('the letter body goes through the sanitizing markdown pipeline and is disposed before re-render', () => {
    assert.match(SOURCE, /import \{ destroyChatMarkdown, enhanceChatMarkdown, mountChatMarkdown \} from '\.\/chat_markdown\.js'/);
    assert.match(SOURCE, /mountChatMarkdown\(letterBody, view\.markdown \|\| ''\)/);
    assert.match(SOURCE, /letterDisposer = enhanceChatMarkdown\(letterBody, \{[\s\S]*?onDomWrite:/);
    // Controls the pipeline adds are scrubbed after EVERY write it makes: a fenced block
    // gets its Copy button at render, a degrading mermaid block gets one asynchronously.
    assert.match(SOURCE, /function stripLetterControls\(\)[\s\S]*?querySelectorAll\('button'\)[\s\S]*?remove\(\)/);
    const enhanceCall = SOURCE.slice(SOURCE.indexOf('letterDisposer = enhanceChatMarkdown'));
    assert.match(enhanceCall.slice(0, 400), /onDomWrite: \(mutate\) => \{\s*mutate\(\);\s*stripLetterControls\(\);/);
    // The disposer (or the module-level destroyer, when none was kept) runs
    // BEFORE the innerHTML that would orphan its charts and timers — on the
    // content path and on the hide path alike.
    assert.match(SOURCE, /function releaseLetterBody\(\)[\s\S]*?letterDisposer\(\)[\s\S]*?destroyChatMarkdown\(letterBody\)/);
    const render = SOURCE.slice(SOURCE.indexOf('function renderLetter()'), SOURCE.indexOf('function render()'));
    assert.ok(render.indexOf('releaseLetterBody()') < render.indexOf('letterBody.innerHTML'), 'release before write');
    assert.equal((render.match(/releaseLetterBody\(\)/g) || []).length, 2, 'hide path and content path both release');
    // Unchanged content keeps its DOM (and the owner's selection with it).
    assert.match(render, /const nextKey = letterContentKey\(view\);\s*if \(nextKey === letterKey\) return;/);
    // Content identity is the CONTENT: two different paragraphs of equal length must not
    // share a key, and a rewrite that produced the same text must not throw the DOM away.
    assert.match(SOURCE, /function letterContentKey\(view\) \{[\s\S]*?return view\.markdown;/);
    assert.doesNotMatch(SOURCE, /view\.markdown\.length/);
});


test('the letter rides the existing render path and never writes the verdict surfaces', () => {
    // No listener, timer or poll of its own: render() already runs on every
    // phase change and status load.
    assert.match(SOURCE, /\]\.includes\(verdict\.state\);\s*\n\s*renderLetter\(\);/);
    assert.equal((SOURCE.match(/renderLetter\(\)/g) || []).length, 3, 'ordinary render and explicit refresh share one renderer');

    const letterCode = SOURCE.slice(SOURCE.indexOf('function releaseLetterBody()'), SOURCE.indexOf('function render()'));
    for (const forbidden of ['dot.dataset.tone', '#updates-summary', 'summary.textContent', 'primaryBtn']) {
        assert.ok(!letterCode.includes(forbidden), `letter code must not touch ${forbidden}`);
    }
    // The apply flow stays exactly what it was: the legacy-path slice that
    // tests/test_packaged_runtime_and_lifecycle.py reads must not gain letter
    // code (or the 'replace'/'stash' literals its own guard forbids).
    const applyFn = SOURCE.split('async function applyUpdate')[1].split('\n    }')[0];
    for (const forbidden of ['letter', 'Letter', "'replace'", "'stash'"]) {
        assert.ok(!applyFn.includes(forbidden), `applyUpdate must not contain ${forbidden}`);
    }
});
