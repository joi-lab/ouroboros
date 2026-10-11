function reviewNodeKey(node) {
    const dataset = node?.dataset || {};
    if (Object.hasOwn(dataset, 'reviewSection')) return 'section';
    if (Object.hasOwn(dataset, 'reviewSectionToggle')) return 'section-toggle';
    if (Object.hasOwn(dataset, 'reviewHydrateStatus')) return 'hydrate-status';
    if (Object.hasOwn(dataset, 'reviewAuthorDecision')) return 'author-decision';
    if (dataset.reviewGroup) return `group:${dataset.reviewGroup}`;
    if (dataset.reviewGroupToggle) return `group-toggle:${dataset.reviewGroupToggle}`;
    if (dataset.reviewAttempt) return `attempt:${dataset.reviewAttempt}`;
    if (dataset.reviewAttemptToggle) return `attempt-toggle:${dataset.reviewAttemptToggle}`;
    if (dataset.reviewAttemptDetail) return `attempt-detail:${dataset.reviewAttemptDetail}`;
    for (const className of [
        'chat-review-groups', 'chat-review-attempts', 'chat-review-attempt-main',
        'chat-review-group-cost', 'chat-review-initiator',
    ]) {
        if (node?.classList?.contains?.(className)) return `class:${className}`;
    }
    return '';
}

function syncReviewAttributes(current, desired, preserve = new Set()) {
    for (const attr of Array.from(current.attributes || [])) {
        if (!preserve.has(attr.name) && !desired.hasAttribute(attr.name)) {
            current.removeAttribute(attr.name);
        }
    }
    for (const attr of Array.from(desired.attributes || [])) {
        if (!preserve.has(attr.name)) current.setAttribute(attr.name, attr.value);
    }
}

function replaceReviewChildren(current, desired) {
    const desiredChildren = Array.from(desired.children || []);
    if (!desiredChildren.length) {
        if (current.innerHTML !== desired.innerHTML) current.innerHTML = desired.innerHTML;
        return;
    }
    const available = Array.from(current.children || []);
    const used = new Set();
    desiredChildren.forEach((desiredChild, index) => {
        const key = reviewNodeKey(desiredChild);
        let match = key
            ? available.find((candidate) => !used.has(candidate) && reviewNodeKey(candidate) === key)
            : (
                !used.has(available[index]) && !reviewNodeKey(available[index])
                    ? available[index]
                    : available.find((candidate) => !used.has(candidate) && !reviewNodeKey(candidate))
            );
        if (match && match.tagName !== desiredChild.tagName) match = null;
        if (!match) {
            match = desiredChild.cloneNode(true);
            current.insertBefore(match, current.children[index] || null);
        } else {
            const atIndex = current.children[index] || null;
            if (match !== atIndex) current.insertBefore(match, atIndex);
            patchReviewElement(match, desiredChild);
        }
        used.add(match);
    });
    for (const child of Array.from(current.children || [])) {
        if (!used.has(child)) child.remove();
    }
}

function patchReviewElement(current, desired) {
    const isDetail = Boolean(current?.dataset?.reviewAttemptDetail);
    const isExactSkillDetail = isDetail && Boolean(current.dataset.skillReviewJob);
    const scrollTop = isDetail ? current.scrollTop : null;
    const preserve = isExactSkillDetail
        ? new Set(['data-state', 'aria-busy', 'tabindex'])
        : new Set();
    syncReviewAttributes(current, desired, preserve);
    if (!isExactSkillDetail) replaceReviewChildren(current, desired);
    if (isDetail && Number.isFinite(scrollTop)) current.scrollTop = scrollTop;
}

export function reconcileReviewElementTree(current, desired) {
    if (!current || !desired || current.tagName !== desired.tagName) return false;
    patchReviewElement(current, desired);
    return true;
}

/**
 * Reconcile the keyed Reviews subtree in place. Stable group/attempt/detail
 * nodes retain lazy state, scroll position and arbitrary focused descendants;
 * only genuine structural additions/removals allocate nodes.
 */
export function reconcileReviewMarkup(host, html) {
    const doc = host?.ownerDocument;
    const template = doc?.createElement?.('template');
    if (!host || !template || !template.content) {
        if (host) host.innerHTML = html;
        return false;
    }
    template.innerHTML = String(html || '').trim();
    const desired = template.content.firstElementChild;
    const current = Array.from(host.children || [])
        .find((child) => Object.hasOwn(child.dataset || {}, 'reviewSection')) || null;
    if (!desired) {
        if (current) current.remove();
        return true;
    }
    if (!current) {
        host.replaceChildren(desired);
        return true;
    }
    reconcileReviewElementTree(current, desired);
    for (const child of Array.from(host.children || [])) {
        if (child !== current) child.remove();
    }
    return true;
}
