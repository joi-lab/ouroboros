// Rule table for the §7.5 hub card verdict (hubflow sprint 2026-08-23,
// repaired for issue #1314: the publish receipt is history, never an action
// gate). Every branch of hubSyncVerdict is pinned: action enum, badge set,
// copy facts.

import assert from 'node:assert/strict';
import test from 'node:test';

import { hubFactsPending, hubListingRowFor, hubSubmissionFacts, hubSyncVerdict } from '../modules/hub_sync.js';
import { renderSubmissionHistory } from '../modules/utils.js';

const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

function catalogRow(overrides = {}) {
    return {
        slug: 'claudexor_quotas',
        sanitized_name: 'claudexor_quotas',
        latest_version: '0.3.0',
        identity_conflict: false,
        ...overrides,
    };
}

function listingRow(overrides = {}) {
    return {
        name: 'claudexor_quotas',
        source: 'ouroboroshub',
        location: 'ouroboroshub',
        version: '0.3.0',
        content_hash: HASH_A,
        official_hub_verified: false,
        published: null,
        published_malformed: false,
        review_stale: false,
        ...overrides,
    };
}

function receipt(overrides = {}) {
    return {
        slug: 'claudexor_quotas',
        version: '0.2.0',
        content_hash: HASH_A,
        repository: 'razzant/ouroboroshub',
        pr_number: 38,
        pr_url: 'https://github.com/razzant/ouroboroshub/pull/38',
        published_at: '2026-08-20T00:00:00Z',
        ...overrides,
    };
}

// ---------------------------------------------------------------------------
// Install / Installed / Update (no local row; hub bucket).
// ---------------------------------------------------------------------------

test('no local occupant with a live catalog row -> install', () => {
    const verdict = hubSyncVerdict(null, catalogRow(), {});
    assert.equal(verdict.action, 'install');
    assert.deepEqual(verdict.badges, []);
    assert.deepEqual(verdict.copy_facts, {
        local_version: '',
        catalog_version: '0.3.0',
        occupying_bucket: null,
        no_receipt: false,
        receipt_unreadable: false,
        submission: null,
    });
});

test('no local occupant and no catalog row -> none (nothing to install from)', () => {
    const verdict = hubSyncVerdict(null, null, {});
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, []);
});

test('hub bucket at the catalog version -> installed', () => {
    const verdict = hubSyncVerdict(listingRow(), catalogRow(), {});
    assert.equal(verdict.action, 'installed');
    assert.deepEqual(verdict.badges, []);
    assert.equal(verdict.copy_facts.local_version, '0.3.0');
    assert.equal(verdict.copy_facts.occupying_bucket, null);
});

test('hub bucket behind the catalog -> update + update_available badge', () => {
    const verdict = hubSyncVerdict(
        listingRow({ version: '0.2.0' }),
        catalogRow({ latest_version: '0.3.0' }),
        {},
    );
    assert.equal(verdict.action, 'update');
    assert.deepEqual(verdict.badges, ['update_available']);
    assert.equal(verdict.copy_facts.local_version, '0.2.0');
    assert.equal(verdict.copy_facts.catalog_version, '0.3.0');
});

test('version comparison is string inequality only (no semver ordering)', () => {
    // '0.10.0' vs '0.9.0' — inequality is all that is claimed; a numerically
    // "older" catalog version still reads as a difference, never as ordering.
    const verdict = hubSyncVerdict(
        listingRow({ version: '0.10.0' }),
        catalogRow({ latest_version: '0.9.0' }),
        {},
    );
    assert.equal(verdict.action, 'update');
});

test('hubFactsPending: only an explicit null is "not known yet"', () => {
    assert.equal(hubFactsPending([{ official_hub_verified: true }, { official_hub_verified: null }]), true);
    assert.equal(hubFactsPending([{ official_hub_verified: false }, { name: 'external-row' }]), false);
    assert.equal(hubFactsPending(undefined), false);
});

test('verified hub bucket -> published badge rides ONLY official_hub_verified===true', () => {
    const verified = hubSyncVerdict(
        listingRow({ official_hub_verified: true }),
        catalogRow(),
        {},
    );
    assert.equal(verified.action, 'installed');
    assert.deepEqual(verified.badges, ['published']);

    // Truthy-but-not-true never counts.
    const truthy = hubSyncVerdict(
        listingRow({ official_hub_verified: 1 }),
        catalogRow(),
        {},
    );
    assert.deepEqual(truthy.badges, []);

    // Verified fact outside the hub bucket never earns the badge.
    const external = hubSyncVerdict(
        listingRow({ location: 'external', official_hub_verified: true }),
        catalogRow(),
        {},
    );
    assert.equal(external.badges.includes('published'), false);
});

test('arbitrary version strings compare only by inequality in both buckets', () => {
    for (const [local, catalog] of [['nightly-7', 'release candidate ☃'], ['2026.09.27', '1'], ['', '0.1.0']]) {
        assert.equal(hubSyncVerdict(listingRow({ version: local }), catalogRow({ latest_version: catalog }), {}).action, 'update');
        assert.equal(hubSyncVerdict(
            listingRow({ location: 'external', version: local }), catalogRow({ latest_version: catalog }), {},
        ).action, 'adopt');
    }
    assert.equal(hubSyncVerdict(listingRow({ version: 'nightly-7' }), catalogRow({ latest_version: 'nightly-7' }), {}).action, 'installed');
    assert.equal(hubSyncVerdict(
        listingRow({ location: 'external', version: 'nightly-7' }), catalogRow({ latest_version: 'nightly-7' }), {},
    ).action, 'adopt');
});

// ---------------------------------------------------------------------------
// Adopt (external occupant) — receipt shapes the copy, never the eligibility.
// ---------------------------------------------------------------------------

test('external occupant with a catalog slug and NO receipt -> adopt with no_receipt warning fact', () => {
    const verdict = hubSyncVerdict(
        listingRow({ location: 'external', source: 'self_authored', version: '0.1.0' }),
        catalogRow(),
        {},
    );
    assert.equal(verdict.action, 'adopt');
    assert.deepEqual(verdict.badges, []);
    assert.equal(verdict.copy_facts.no_receipt, true);
    assert.equal(verdict.copy_facts.receipt_unreadable, false);
    assert.equal(verdict.copy_facts.submission, null);
    assert.equal(verdict.copy_facts.occupying_bucket, 'external');
});

test('external occupant with receipt, hash match, catalog serves the published version -> adopt', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            version: '0.2.0',
            content_hash: HASH_A,
            published: receipt({ version: '0.2.0', content_hash: HASH_A }),
        }),
        catalogRow({ latest_version: '0.2.0' }),
        {},
    );
    // Served at the submitted version: adopt moves the bucket even at hash match.
    assert.equal(verdict.action, 'adopt');
    assert.deepEqual(verdict.badges, []);
    assert.equal(verdict.copy_facts.no_receipt, false);
    assert.deepEqual(verdict.copy_facts.submission, {
        version: '0.2.0', pr_number: 38, pr_url: 'https://github.com/razzant/ouroboroshub/pull/38', local_differs: false,
    });
});

test('external occupant edited since submission -> adopt, local_differs history and no badge', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            version: '0.2.0',
            content_hash: HASH_B,
            published: receipt({ version: '0.2.0', content_hash: HASH_A }),
        }),
        catalogRow({ latest_version: '0.3.0' }),
        {},
    );
    assert.equal(verdict.action, 'adopt');
    assert.deepEqual(verdict.badges, []);
    assert.equal(verdict.copy_facts.submission.local_differs, true);
    assert.equal(verdict.copy_facts.submission.version, '0.2.0');
});

// ---------------------------------------------------------------------------
// #1314 — a receipt/catalog version difference never vetoes the catalog copy.
// It cannot tell a pending update PR from a merged one the catalog has since
// moved past, so the confirm dialog, not a guess, owns the replacement.
// ---------------------------------------------------------------------------

for (const [label, submitted, served] of [
    ['catalog moved past the submission (1.1.2 -> 1.1.3)', '1.1.2', '1.1.3'],
    ['submission not served yet (pending 0.4.0, catalog 0.3.0)', '0.4.0', '0.3.0'],
]) {
    test(`unedited submission, ${label} -> adopt with the receipt as history`, () => {
        const verdict = hubSyncVerdict(
            listingRow({
                location: 'external',
                version: submitted,
                content_hash: HASH_A,
                published: receipt({ version: submitted, content_hash: HASH_A, pr_number: 42 }),
            }),
            catalogRow({ latest_version: served }),
            {},
        );
        assert.equal(verdict.action, 'adopt');
        assert.deepEqual(verdict.badges, []);
        assert.equal(verdict.copy_facts.catalog_version, served);
        assert.deepEqual(verdict.copy_facts.submission, {
            version: submitted, pr_number: 42, pr_url: 'https://github.com/razzant/ouroboroshub/pull/38', local_differs: false,
        });
    });
}

test('local, submitted and served versions all differ -> adopt; history keeps the SUBMITTED version', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            version: '1.2.0',
            content_hash: HASH_B,
            published: receipt({ version: '1.1.0', content_hash: HASH_A }),
        }),
        catalogRow({ latest_version: '1.3.0' }),
        {},
    );
    assert.equal(verdict.action, 'adopt');
    assert.equal(verdict.copy_facts.local_version, '1.2.0');
    assert.equal(verdict.copy_facts.catalog_version, '1.3.0');
    assert.equal(verdict.copy_facts.submission.version, '1.1.0');
    assert.equal(verdict.copy_facts.submission.local_differs, true);
});

test('hub-installed copy keeps Installed/Update whatever the receipt says', () => {
    const published = receipt({ version: '0.2.11', content_hash: HASH_B });
    const behind = hubSyncVerdict(listingRow({ version: '0.2.11', published }), catalogRow({ latest_version: '0.2.12' }), {});
    assert.equal(behind.action, 'update');
    assert.deepEqual(behind.badges, ['update_available']);
    assert.equal(behind.copy_facts.submission.version, '0.2.11');
    const served = hubSyncVerdict(listingRow({ version: '0.2.12', published }), catalogRow({ latest_version: '0.2.12' }), {});
    assert.equal(served.action, 'installed');
    assert.deepEqual(served.badges, []);
    assert.equal(served.copy_facts.submission.pr_number, 38, 'the PR stays reachable when versions match');
});

test('a structurally valid foreign-looking receipt is history, never ownership or a gate', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            published: receipt({ slug: 'someone_else', repository: 'other/hub', pr_url: 'https://example.com/pr/9', pr_number: 9 }),
        }),
        catalogRow({ latest_version: '9.9.9' }),
        {},
    );
    assert.equal(verdict.action, 'adopt');
    assert.equal(verdict.copy_facts.no_receipt, false);
    assert.equal(verdict.copy_facts.submission.pr_number, 9);
});

test('receipt with slug absent from the catalog -> no action; the submission is history only', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            version: '0.1.0',
            content_hash: HASH_A,
            published: receipt({ version: '0.1.0', content_hash: HASH_A, pr_number: 55 }),
        }),
        null,
        {},
    );
    // No catalog row: nothing to adopt/install, and no waiting state claimed.
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, []);
    assert.equal(verdict.copy_facts.submission.pr_number, 55);
});

test('edited local copy with slug absent from the catalog -> none, history says the files differ', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            content_hash: HASH_B,
            published: receipt({ content_hash: HASH_A }),
        }),
        null,
        {},
    );
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, []);
    assert.equal(verdict.copy_facts.submission.local_differs, true);
});

// ---------------------------------------------------------------------------
// Occupied by non-adoptable buckets -> honest none cards.
// ---------------------------------------------------------------------------

for (const bucket of ['native', 'user_repo', 'clawhub']) {
    test(`${bucket} occupant -> none with occupying_bucket fact`, () => {
        const verdict = hubSyncVerdict(
            listingRow({ location: bucket, source: bucket === 'clawhub' ? 'clawhub' : bucket }),
            catalogRow(),
            {},
        );
        assert.equal(verdict.action, 'none');
        assert.deepEqual(verdict.badges, []);
        assert.equal(verdict.copy_facts.occupying_bucket, bucket);
    });
}

test('unknown/empty location -> none (fail closed, no invented action)', () => {
    const verdict = hubSyncVerdict(listingRow({ location: '' }), catalogRow(), {});
    assert.equal(verdict.action, 'none');
    assert.equal(verdict.copy_facts.occupying_bucket, null);
});

// ---------------------------------------------------------------------------
// Catalog identity conflict -> contract-error card, no actions.
// ---------------------------------------------------------------------------

test('identity_conflict -> conflict badge and action none, even for an installable pair', () => {
    const notInstalled = hubSyncVerdict(null, catalogRow({ identity_conflict: true }), {});
    assert.equal(notInstalled.action, 'none');
    assert.deepEqual(notInstalled.badges, ['conflict']);

    const hubBucket = hubSyncVerdict(
        listingRow({ version: '0.1.0', official_hub_verified: true }),
        catalogRow({ identity_conflict: true }),
        {},
    );
    assert.equal(hubBucket.action, 'none');
    // No update claims off a conflicted catalog row; the listing-plane
    // published fact stays (it is server-verified, not a catalog comparison).
    assert.deepEqual(hubBucket.badges, ['published', 'conflict']);

    const submitted = hubSyncVerdict(
        listingRow({ location: 'external', published: receipt() }),
        catalogRow({ identity_conflict: true }),
        {},
    );
    assert.equal(submitted.action, 'none');
    assert.deepEqual(submitted.badges, ['conflict']);
    assert.equal(submitted.copy_facts.submission.pr_number, 38, 'history is a local fact, not an action');
});

// ---------------------------------------------------------------------------
// Availability flags — fetch failures never impersonate facts.
// ---------------------------------------------------------------------------

test('catalogUnavailable -> never install, catalog_unavailable badge', () => {
    const verdict = hubSyncVerdict(null, null, { catalogUnavailable: true });
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, ['catalog_unavailable']);
});

test('catalogUnavailable with a hub-bucket row -> installed stays a local fact, no update claim', () => {
    const verdict = hubSyncVerdict(
        listingRow({ version: '0.2.0', published: receipt() }),
        null,
        { catalogUnavailable: true },
    );
    assert.equal(verdict.action, 'installed');
    // Only the honest unavailability badge; the submission history is a
    // local fact and stays independent of the catalog read.
    assert.deepEqual(verdict.badges, ['catalog_unavailable']);
    assert.equal(verdict.copy_facts.catalog_version, '');
    assert.equal(verdict.copy_facts.submission.version, '0.2.0');
});

test('catalogUnavailable with an external submission -> no adopt guess, history kept', () => {
    // An outage is not an absent row: even a row passed by mistake is ignored.
    for (const row of [null, catalogRow()]) {
        const verdict = hubSyncVerdict(
            listingRow({ location: 'external', content_hash: HASH_A, published: receipt() }),
            row,
            { catalogUnavailable: true },
        );
        assert.equal(verdict.action, 'none');
        assert.deepEqual(verdict.badges, ['catalog_unavailable']);
        assert.equal(verdict.copy_facts.catalog_version, '');
        assert.equal(verdict.copy_facts.submission.pr_number, 38);
    }
});

test('listingUnavailable -> never Install, listing_unavailable badge, local claims dropped', () => {
    const verdict = hubSyncVerdict(null, catalogRow(), { listingUnavailable: true });
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, ['listing_unavailable']);
    assert.equal(verdict.copy_facts.no_receipt, false);

    // Even a stale listing row passed by mistake is ignored: no local facts.
    const withRow = hubSyncVerdict(listingRow(), catalogRow(), { listingUnavailable: true });
    assert.equal(withRow.action, 'none');
    assert.equal(withRow.copy_facts.local_version, '');
    assert.deepEqual(withRow.badges, ['listing_unavailable']);
});

test('both fetches down -> both badges in the frozen order', () => {
    const verdict = hubSyncVerdict(null, null, { catalogUnavailable: true, listingUnavailable: true });
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, ['catalog_unavailable', 'listing_unavailable']);
});

// ---------------------------------------------------------------------------
// Malformed receipt -> distinct copy fact, not the "someone else's" warning.
// ---------------------------------------------------------------------------

test('published_malformed -> receipt_unreadable fact, no_receipt stays false, adopt still offered', () => {
    const verdict = hubSyncVerdict(
        listingRow({ location: 'external', published: null, published_malformed: true }),
        catalogRow(),
        {},
    );
    assert.equal(verdict.action, 'adopt');
    assert.equal(verdict.copy_facts.receipt_unreadable, true);
    assert.equal(verdict.copy_facts.no_receipt, false);
    assert.equal(verdict.copy_facts.submission, null);
    assert.deepEqual(verdict.badges, []);
});

test('absent and explicitly cleared receipts both read as no record; adopt is never gated on them', () => {
    // The listing projects both absence and an owner Clear as published=null.
    const verdict = hubSyncVerdict(listingRow({ location: 'external', published: null }), catalogRow(), {});
    assert.equal(verdict.action, 'adopt');
    assert.equal(verdict.copy_facts.no_receipt, true);
    assert.equal(verdict.copy_facts.receipt_unreadable, false);
    assert.equal(verdict.copy_facts.submission, null);
});

// ---------------------------------------------------------------------------
// hubSubmissionFacts + the shared history line.
// ---------------------------------------------------------------------------

test('hubSubmissionFacts reads the receipt, not the local copy', () => {
    assert.equal(hubSubmissionFacts(null), null);
    assert.equal(hubSubmissionFacts(listingRow()), null);
    const facts = hubSubmissionFacts(listingRow({ version: '9.0.0', content_hash: '', published: receipt({ pr_number: 1.5 }) }));
    assert.deepEqual(facts, {
        version: '0.2.0', pr_number: null, pr_url: 'https://github.com/razzant/ouroboroshub/pull/38',
        // An unknown local hash claims no difference.
        local_differs: false,
    });
});

test('renderSubmissionHistory links only a safe URL and keeps the other facts', () => {
    const safe = renderSubmissionHistory({ version: '1.1.2', pr_number: 60, pr_url: 'https://github.com/razzant/OuroborosHub/pull/60', local_differs: true });
    assert.match(safe, /^Submitted v1\.1\.2 · <a href="https:\/\/github\.com\/razzant\/OuroborosHub\/pull\/60" target="_blank" rel="noopener noreferrer">PR #60<\/a> · Local files differ from the submitted copy$/);
    for (const url of ['javascript:alert(1)', 'data:text/html,x', '" onmouseover="x', '']) {
        const html = renderSubmissionHistory({ version: '<b>1</b>', pr_number: 7, pr_url: url, local_differs: false });
        assert.equal(html, 'Submitted v&lt;b&gt;1&lt;/b&gt; · PR #7');
    }
    assert.equal(renderSubmissionHistory({ version: '', pr_number: null, pr_url: '', local_differs: false }), 'Submitted');
    assert.equal(renderSubmissionHistory(null), '');
});

// ---------------------------------------------------------------------------
// hubListingRowFor — /api/extensions row -> §7.5 listing-row projection.
// ---------------------------------------------------------------------------

test('hubListingRowFor prefers the server location and coerces types', () => {
    const row = hubListingRowFor({
        name: 'quotas',
        source: 'ouroboroshub',
        location: 'ouroboroshub',
        payload_root: 'skills/external/quotas',
        version: '0.3.0',
        content_hash: HASH_A,
        official_hub_verified: true,
        published: receipt(),
        published_malformed: false,
        review_stale: false,
    });
    assert.equal(row.location, 'ouroboroshub');
    assert.equal(row.official_hub_verified, true);
    assert.equal(row.published.pr_number, 38);
});

test('hubListingRowFor derives the bucket from payload_root when location is absent', () => {
    for (const bucket of ['external', 'clawhub', 'ouroboroshub']) {
        const row = hubListingRowFor({
            name: 'quotas',
            source: 'self_authored',
            payload_root: `skills/${bucket}/quotas`,
            version: '0.1.0',
        });
        assert.equal(row.location, bucket);
    }
});

test('hubListingRowFor falls back to the source tag only for repo-plane buckets', () => {
    assert.equal(hubListingRowFor({ name: 'x', source: 'native' }).location, 'native');
    assert.equal(hubListingRowFor({ name: 'x', source: 'user_repo' }).location, 'user_repo');
    // A data-plane tag without a payload_root claims nothing.
    assert.equal(hubListingRowFor({ name: 'x', source: 'self_authored' }).location, '');
    assert.equal(hubListingRowFor(null), null);
});

test('hubListingRowFor normalizes junk published/malformed fields', () => {
    const row = hubListingRowFor({
        name: 'x',
        source: 'external',
        payload_root: 'skills/external/x',
        published: 'not-an-object',
        published_malformed: 'yes',
        review_stale: 1,
    });
    assert.equal(row.published, null);
    assert.equal(row.published_malformed, false);
    assert.equal(row.review_stale, false);
});

test('identity_collision on the listing row fails closed to a no-action conflict', () => {
    const listing = {
        name: 'demo', source: 'external', location: 'external', version: '1.0.0',
        content_hash: 'a'.repeat(64), official_hub_verified: false,
        published: null, published_malformed: false, review_stale: false,
        identity_collision: true,
    };
    const catalog = { slug: 'demo', sanitized_name: 'demo', latest_version: '2.0.0', identity_conflict: false };
    const verdict = hubSyncVerdict(listing, catalog, {});
    assert.equal(verdict.action, 'none');
    assert.ok(verdict.badges.includes('conflict'));
});

test('hubListingRowFor carries the identity_collision flag', () => {
    const row = hubListingRowFor({
        name: 'demo', source: 'external', payload_root: 'skills/external/demo',
        version: '1.0.0', content_hash: 'a'.repeat(64), identity_collision: true,
    });
    assert.equal(row.identity_collision, true);
    const clean = hubListingRowFor({ name: 'demo', source: 'external', payload_root: 'skills/external/demo' });
    assert.equal(clean.identity_collision, false);
});

test('native-located payload without seed marker maps to a named no-action card', () => {
    // Legacy user-managed payload physically under skills/native/ (no .seed-origin):
    // source reads logical "external", but the LOCATION is native — no adopt, and
    // the occupying bucket is named for the card copy.
    const row = hubListingRowFor({
        name: 'weather', source: 'external', payload_root: 'skills/native/weather',
        version: '0.1.0', content_hash: 'a'.repeat(64),
    });
    assert.equal(row.location, 'native');
    const verdict = hubSyncVerdict(row, { slug: 'weather', sanitized_name: 'weather', latest_version: '0.3.2', identity_conflict: false }, {});
    assert.equal(verdict.action, 'none');
    assert.equal(verdict.copy_facts.occupying_bucket, 'native');
});
