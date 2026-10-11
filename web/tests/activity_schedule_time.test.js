import assert from 'node:assert/strict';
import test from 'node:test';

// The DOM-free helper; the rendered Activity page is exercised by the browser lane.
globalThis.document = globalThis.document || { createElement: () => ({}) };
const { scheduleInstantHtml } = await import('../modules/activity.js');

test('a stored UTC instant renders in the viewer zone with the exact UTC beside it', () => {
    const html = scheduleInstantHtml('2027-01-15T09:00:00+00:00', { timeZone: 'Asia/Tokyo' });
    assert.match(html, /^<time datetime="2027-01-15T09:00:00Z" title="2027-01-15T09:00:00Z">/);
    // The viewer's own locale decides 12/24-hour display; the instant is what matters.
    assert.match(html, /(18:00|06:00\sPM)/, 'the Tokyo viewer sees 18:00 local');
    assert.match(html, /\(Jan 15, 09:00(\sAM)? UTC\)<\/time>$/);
    const newYork = scheduleInstantHtml('2027-07-15T09:00:00Z', { timeZone: 'America/New_York' });
    assert.match(newYork, /05:00(\sAM)?/);
    assert.match(newYork, /\(Jul 15, 09:00(\sAM)? UTC\)/);
});

test('an unparseable or absent value is shown raw, never guessed', () => {
    assert.equal(scheduleInstantHtml('tomorrow <9am>'), 'tomorrow &lt;9am&gt;');
    assert.equal(scheduleInstantHtml(''), '');
    assert.equal(scheduleInstantHtml(undefined), '');
});

test('a hard deadline includes its year in both viewer and UTC text', () => {
    const html = scheduleInstantHtml('2099-01-01T00:00:00Z', { timeZone: 'Asia/Tokyo', includeYear: true });
    assert.equal((html.match(/2099/g) || []).length, 4); // datetime, title and both visible instants
});
