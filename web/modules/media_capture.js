/** One page-wide capture owner, shared by dictation and future live sessions.
 * acquire() calls getUserMedia before returning to the gesture handler. A client
 * recognizer uses capture:false: it opens its own microphone under the same lease.
 * Camera and screen kinds are reserved; they currently return no_api.
 */
export function isLoopback(host = '') {
    const name = host.toLowerCase().replace(/^\[|\]$/g, '');
    if (name === 'localhost' || name === '::1') return true;
    const parts = name.split('.');
    return parts.length === 4 && parts[0] === '127'
        && parts.every(part => /^\d{1,3}$/.test(part) && Number(part) <= 255);
}

export function captureAvailability(env = globalThis, { kind = 'audio', capture = true } = {}) {
    if (kind !== 'audio') return { ok: false, code: 'no_api' };
    if (!env.isSecureContext && !isLoopback(env.location?.hostname))
        return { ok: false, code: 'insecure_origin' };
    if (capture && typeof env.navigator?.mediaDevices?.getUserMedia !== 'function')
        return { ok: false, code: 'no_api' };
    return { ok: true };
}

export function mediaFailure(error) {
    if (['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(error?.name)) return 'denied';
    if (['NotFoundError', 'DevicesNotFoundError', 'OverconstrainedError'].includes(error?.name)) return 'no_device';
    return 'lost';
}

export function captureReason(code, label = '') {
    return {
        in_use: `The microphone is in use${label ? ` by ${label}` : ''}.`,
        no_api: 'Microphone capture is unavailable in this browser or desktop app.',
        insecure_origin: 'Voice input needs a secure connection (https or this computer).',
        denied: 'Microphone access was denied. Allow it in your browser or system settings.',
        no_device: 'No microphone is available. Connect one and try again.',
        lost: 'The microphone disconnected. Connect it and try again.',
    }[code] || 'Microphone capture stopped.';
}

const stopTracks = stream => stream?.getTracks().forEach(track => track.stop());

export function createCaptureLease(env = globalThis) {
    let holder = null;
    const listeners = [];
    // Panic (chat_activity.js raises ouro:panic after its /panic send) ends any capture at once.
    env.addEventListener?.('ouro:panic', () => releaseAll('panic'));
    function listen(target, event, fn) {
        target?.addEventListener?.(event, fn);
        listeners.push(() => target?.removeEventListener?.(event, fn));
    }
    function releaseAll(reason = 'cancelled') { holder?.release(reason); }
    function watchPage() {
        listen(env, 'pagehide', () => releaseAll('hidden'));
        listen(env.document, 'visibilitychange', () => {
            if (env.document.hidden) releaseAll('hidden');
        });
    }
    function acquire({ kind = 'audio', owner, label = '', constraints = {}, capture = true } = {}) {
        if (holder) return { code: 'in_use', label: holder.label, owner: holder.owner };
        const available = captureAvailability(env, { kind, capture });
        if (!available.ok) return { code: available.code };
        const abort = new AbortController();
        let request = 0;
        let unwatch = () => {};
        const handle = {
            kind, owner, label, stream: null, signal: abort.signal, fellBack: false,
            release(reason = 'cancelled') {
                if (holder !== handle) return;
                holder = null;
                request++;
                unwatch();
                stopTracks(handle.stream);
                handle.stream = null;
                for (const off of listeners.splice(0)) off();
                abort.abort(reason);
            },
            replace(next = {}) {
                if (holder !== handle) return Promise.resolve({ code: 'lost' });
                if (!capture) return Promise.resolve({ code: 'no_api' });
                return open(next);
            },
        };
        holder = handle;
        watchPage();
        function adopt(stream, ticket, fellBack) {
            if (holder !== handle || ticket !== request) {
                stopTracks(stream);
                return { code: 'lost' };
            }
            unwatch();
            stopTracks(handle.stream);
            handle.stream = stream;
            handle.fellBack = fellBack;
            const ended = () => handle.release('lost');
            const tracks = stream.getTracks();
            tracks.forEach(track => track.addEventListener('ended', ended));
            unwatch = () => tracks.forEach(track => track.removeEventListener('ended', ended));
            if (tracks.some(track => track.readyState === 'ended')) {
                handle.release('lost');
                return { code: 'lost' };
            }
            return stream;
        }
        function open(audio) {
            const ticket = ++request;
            const media = env.navigator.mediaDevices;
            // This call, including replace(), stays before the first await.
            let pending;
            try { pending = media.getUserMedia({ audio, video: false }); }
            catch (error) { pending = Promise.reject(error); }
            return Promise.resolve(pending).then(stream => adopt(stream, ticket, false), async error => {
                if (holder !== handle || ticket !== request) return { code: 'lost' };
                if (audio.deviceId && ['OverconstrainedError', 'NotFoundError'].includes(error?.name)) {
                    const { deviceId: _removed, ...defaults } = audio;
                    try { return adopt(await media.getUserMedia({ audio: defaults, video: false }), ticket, true); }
                    catch (fallbackError) { return { code: mediaFailure(fallbackError) }; }
                }
                return { code: mediaFailure(error) };
            });
        }
        if (!capture) return handle;
        const pending = open(constraints).then(result => {
            if (result.code) { handle.release(result.code); return result; }
            return handle;
        });
        // Cancellation while a permission prompt is open releases the reservation
        // immediately; a late stream is stopped by adopt().
        pending.release = handle.release;
        return pending;
    }
    return { acquire, releaseAll, currentHolder: () => holder
        ? { kind: holder.kind, owner: holder.owner, label: holder.label } : null };
}

const pageCapture = createCaptureLease();
export const acquire = options => pageCapture.acquire(options);
export const currentHolder = () => pageCapture.currentHolder();
// Panic already releases every holder; room teardown calls its own handle.release().
export const releaseAll = reason => pageCapture.releaseAll(reason);
