/**
 * OuroborosHub card verdict — the ONE client-side authority joining a hub
 * catalog row with the global /api/extensions listing row for the same
 * canonical name (plan §7.5, repaired for issue #1314).
 *
 * Pure data-in/data-out: no fetches, no DOM, no version parsing. The only
 * comparisons are string inequality on versions and strict equality between
 * the local content hash and the publish-receipt hash. The local content hash
 * is NEVER compared with the catalog (the payload sidecar is part of the
 * hash, so listing-vs-catalog byte equality is structurally false; that
 * equality lives server-side in the official_hub review profile and reaches
 * this function only as the `official_hub_verified` fact).
 *
 * The publish receipt is history, never an action gate: a catalog row proves
 * neither that a particular pull request merged nor who owns the served copy,
 * and a receipt/catalog version difference cannot tell a pending update PR
 * from a merged one the catalog has since moved past. So the action comes
 * from the local location and catalog presence alone, and the receipt reaches
 * the cards as quiet `submission` facts beside it.
 *
 * @typedef {Object} HubListingRow  One /api/extensions skill row projection.
 * @property {string} name               canonical skill name
 * @property {string} source            classification tag (self_authored, …)
 * @property {string} location          physical bucket: external|clawhub|ouroboroshub|native|user_repo|''
 * @property {string} version            local manifest version
 * @property {string} content_hash       loader content hash of the local tree
 * @property {boolean} official_hub_verified byte-exact match with the hub catalog (server fact; the raw
 *   listing field is null while the server holds no fresh catalog view — see hubFactsPending)
 * @property {Object|null} published     publish receipt section (slug, version, content_hash, pr_number, pr_url, …)
 * @property {boolean} published_malformed  receipt exists on disk but is unreadable (server projects published=null)
 * @property {boolean} review_stale
 *
 * @typedef {Object} HubCatalogRow  One /api/marketplace/ouroboroshub/catalog row projection.
 * @property {string} slug
 * @property {string} sanitized_name    server-computed canonical name (JS never sanitizes)
 * @property {string} latest_version
 * @property {boolean} identity_conflict catalog holds >1 slug with this canonical name
 *
 * @typedef {Object} HubSubmission  What this installation last submitted (the receipt's own facts).
 * @property {string} version           the SUBMITTED version, never the current local version
 * @property {number|null} pr_number
 * @property {string} pr_url            raw receipt URL; renderers pass it through safeExternalHrefAttr
 * @property {boolean} local_differs    the local content hash differs from the submitted hash
 *   (edits, but also an install/adopt sidecar — never proof of manual edits)
 *
 * @typedef {Object} HubSyncVerdict
 * @property {'install'|'installed'|'update'|'adopt'|'none'} action
 * @property {Array<'published'|'update_available'|'catalog_unavailable'|'listing_unavailable'|'conflict'>} badges
 * @property {{local_version: string, catalog_version: string, occupying_bucket: string|null,
 *            no_receipt: boolean, receipt_unreadable: boolean, submission: HubSubmission|null}} copy_facts
 *   `no_receipt` (no local record) and `receipt_unreadable` (a record that
 *   fails validation) stay distinct; an explicitly cleared record reads as
 *   no record, because clearing is the owner deliberately forgetting it.
 */

/**
 * Project one /api/extensions skill row into the §7.5 listing-row shape.
 * Prefers a server-provided `location`; otherwise derives the physical bucket
 * from `payload_root` (skills/<bucket>/…) and falls back to the source tag
 * only for the repo-plane buckets that have no data-plane payload_root.
 * @returns {HubListingRow|null}
 */
export function hubListingRowFor(skill) {
    if (!skill || typeof skill !== 'object') return null;
    return {
        name: String(skill.name || ''),
        source: String(skill.source || ''),
        location: listingLocation(skill),
        version: String(skill.version || ''),
        content_hash: String(skill.content_hash || ''),
        official_hub_verified: skill.official_hub_verified === true,
        published: skill.published && typeof skill.published === 'object' ? skill.published : null,
        published_malformed: skill.published_malformed === true,
        review_stale: skill.review_stale === true,
        identity_collision: skill.identity_collision === true,
    };
}

/**
 * True while a listing carries a hub fact the server could not know yet:
 * /api/extensions is a local read that never waits for the hub catalog, so
 * `official_hub_verified` is null until a catalog read has landed. The caller
 * re-reads the listing once after its own catalog read settles.
 */
export function hubFactsPending(skills) {
    return Array.isArray(skills) && skills.some((skill) => skill?.official_hub_verified === null);
}

/**
 * Submission history from a listing row's publish receipt, or null without
 * one. Independent of the catalog and of the card action; a structurally
 * valid receipt for a foreign-looking slug/repository is still only history.
 * @param {HubListingRow|null} listingRow
 * @returns {HubSubmission|null}
 */
export function hubSubmissionFacts(listingRow) {
    const published = listingRow && listingRow.published && typeof listingRow.published === 'object'
        ? listingRow.published
        : null;
    if (!published) return null;
    const localHash = String(listingRow.content_hash || '');
    return {
        version: String(published.version || ''),
        pr_number: Number.isInteger(published.pr_number) && published.pr_number > 0 ? published.pr_number : null,
        pr_url: String(published.pr_url || ''),
        local_differs: Boolean(localHash) && localHash !== String(published.content_hash || ''),
    };
}

function listingLocation(skill) {
    const explicit = String(skill.location || '');
    if (explicit) return explicit;
    const bucket = /^skills\/(external|clawhub|ouroboroshub|native)\//.exec(String(skill.payload_root || ''));
    if (bucket) return bucket[1];
    const source = String(skill.source || '').toLowerCase();
    if (source === 'native' || source === 'user_repo') return source;
    return '';
}

/**
 * Compute the card verdict for one (listing row, catalog row) pair.
 *
 * @param {HubListingRow|null} listingRow local occupant of the canonical name, or null
 * @param {HubCatalogRow|null} catalogRow catalog entry for the slug, or null (slug absent)
 * @param {{catalogUnavailable?: boolean, listingUnavailable?: boolean}} [flags]
 * @returns {HubSyncVerdict}
 */
export function hubSyncVerdict(listingRow, catalogRow, flags = {}) {
    const catalogUnavailable = flags.catalogUnavailable === true;
    const listingUnavailable = flags.listingUnavailable === true;
    // A failed listing fetch means no local fact may be claimed at all.
    const listing = listingUnavailable ? null : (listingRow || null);
    const catalog = catalogUnavailable ? null : (catalogRow || null);
    // Conflict is fail-closed from EITHER plane: a catalog whose slugs collide
    // on one canonical name, or a local listing row the loader marked as an
    // identity collision (several same-name occupants — no affordance may act
    // on an ambiguous identity).
    const conflict = Boolean(
        (catalog && catalog.identity_conflict === true)
        || (listing && listing.identity_collision === true),
    );

    const location = listing ? String(listing.location || '') : '';
    const localVersion = listing ? String(listing.version || '') : '';
    const catalogVersion = catalog ? String(catalog.latest_version || '') : '';
    const submission = hubSubmissionFacts(listing);

    const copy_facts = {
        local_version: localVersion,
        catalog_version: catalogVersion,
        occupying_bucket: listing && location && location !== 'ouroboroshub' ? location : null,
        no_receipt: Boolean(listing && !submission && listing.published_malformed !== true),
        receipt_unreadable: Boolean(listing && listing.published_malformed === true),
        submission,
    };

    let action = 'none';
    if (!listingUnavailable && !conflict) {
        if (!listing) {
            // No local occupant → Install (only from a live catalog row).
            if (catalog) action = 'install';
        } else if (location === 'ouroboroshub') {
            // Hub bucket: Installed, or Update when the live catalog version
            // differs (string inequality only — no ordering semantics).
            action = catalog && catalogVersion !== localVersion ? 'update' : 'installed';
        } else if (location === 'external' && catalog) {
            // An available catalog row can replace the local copy whatever the
            // receipt says and whichever way the versions differ; the confirm
            // dialog names the replacement and the Adopt CAS guards the bytes.
            action = 'adopt';
        }
        // clawhub (v1 unsupported), native, user_repo, unknown → 'none'.
    }

    const badges = [];
    if (listing && location === 'ouroboroshub' && listing.official_hub_verified === true) {
        // "Published vX" rides ONLY on the server's byte-exact verification.
        badges.push('published');
    }
    if (!conflict && listing && location === 'ouroboroshub' && catalog
        && catalogVersion !== localVersion) {
        badges.push('update_available');
    }
    if (catalogUnavailable) badges.push('catalog_unavailable');
    if (listingUnavailable) badges.push('listing_unavailable');
    if (conflict) badges.push('conflict');

    return { action, badges, copy_facts };
}
