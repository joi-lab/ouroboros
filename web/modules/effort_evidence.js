// Presentation only: resolution describes a prepared option, never dispatch.
export function effortEvidenceText(evt) {
    const raw = evt.effort || evt.usage?.effort;
    const fact = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : null;
    const resolution = evt.effort_resolution || evt.usage?.effort_resolution
        || evt.observed_attempt?.effort_resolution;
    if (!fact && !resolution) return '';
    const parts = [fact ? `Effort requested ${fact.requested ?? 'unknown'}`
        : `Engine requested effort ${resolution.requested ?? 'unknown'}`];
    const sent = fact?.sent_state === 'omitted' ? 'omitted'
        : fact?.sent && typeof fact.sent === 'object' ? JSON.stringify(fact.sent) : 'unknown';
    parts.push(`host sent ${sent}`);
    if (resolution) {
        const prepared = resolution.submitted == null ? 'omitted'
            : `${resolution.parameter}=${resolution.submitted}`;
        parts.push(`prepared ${prepared} (${resolution.resolution}; ${resolution.source})`);
    }
    const hasObserved = resolution?.observed != null && resolution?.observedSource;
    const observed = hasObserved ? resolution.observed : fact?.report_source ? fact.reported : null;
    const source = hasObserved ? resolution.observedSource : fact?.report_source;
    parts.push(`reported ${observed == null ? 'unknown' : `${observed}${source ? ` (${source})` : ''}`}`);
    return parts.join(' · ');
}
