// Skills' trusted page code (docs/CREATING_SKILLS.md "Page modules", CLIENT_CONTRACT §11): every live contribution
// with `render.kind: "host"` is imported from its revision URL and mounted with one small, versioned context, never
// the composer or the feed (placements are the way into those). A module that fails to load, throws, rejects or does
// not start within the host's module bound is off for this page: its providers are unregistered, one notice names
// its skill, and it stays off until the page reloads or the skill's revision changes. A module is disposed when its
// skill leaves or changes revision, on this page's Panic and when the page goes away.
//
// Recovery never depends on the modules. A boot marker stands in storage while modules mount; a page that finds one
// (the last start did not finish: a frozen or crashed page, however long ago) starts without them, and so does a page
// opened with `?plugins=off`. Either start says so and offers the way back; only Start with plugins or a start that
// finishes clears the marker. The chat header's More menu (every surface) and the macOS app menu reload the page
// with `?plugins=off`.
import { fmt, tr } from './i18n.js';
import { showToast } from './toast.js';
import { boundedText } from './utils.js';
import { WIDGET_REQUEST_TIMEOUT_MS } from './widget_job.js';

export const HOST_MODULE_API = 1;
export const BOOT_MARKER_KEY = 'ouroboros.hostModules.boot';
const PLUGINS_PARAM = 'plugins';
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

/** `href` with page modules switched off (`on` false) or back on; the reconnect banner's reload markers dropped. */
export function pluginsUrl(href, on) {
    const url = new URL(href);
    url.searchParams.delete('_ouro_refresh');
    url.searchParams.delete('_ouro_reason');
    if (on) url.searchParams.delete(PLUGINS_PARAM);
    else url.searchParams.set(PLUGINS_PARAM, 'off');
    return url.toString();
}

export function pluginsOffRequested(href) {
    try { return new URL(href).searchParams.get(PLUGINS_PARAM) === 'off'; } catch { return false; }
}

/** Where the page runs, as a module may need it: a secure context, and this computer (a loopback host). */
export function originFacts(win = globalThis.window) {
    const host = String(win?.location?.hostname || '').toLowerCase();
    return { secure: win?.isSecureContext === true, loopback: LOOPBACK_HOSTS.has(host) || host.endsWith('.localhost') };
}

/** The boot marker in localStorage, or sessionStorage where local storage is refused; every access may throw. */
function bootMarker(win, now) {
    const stores = [];
    for (const name of ['localStorage', 'sessionStorage']) {
        try { if (win?.[name]) stores.push(win[name]); } catch { /* storage blocked */ }
    }
    return {
        read() {
            for (const store of stores) {
                try {
                    const marker = JSON.parse(store.getItem(BOOT_MARKER_KEY) || 'null');
                    if (Number.isFinite(marker?.at)) return marker;
                } catch { /* unreadable: no marker here */ }
            }
            return null;
        },
        write(keys) {
            for (const store of stores) {
                try { store.setItem(BOOT_MARKER_KEY, JSON.stringify({ at: now(), keys })); return true; } catch { /* next */ }
            }
            return false;
        },
        clear() {
            for (const store of stores) {
                try { store.removeItem(BOOT_MARKER_KEY); } catch { /* blocked */ }
            }
        },
    };
}

/**
 * The page-module runtime over a contributions store (contributions.js). `draft` is the visible room's draft port,
 * `notice(text, tone, options)` a toast; `importModule`, `now` and `mountTimeoutMs` are seams for tests.
 */
export function createHostModules({ store, draft = null, win = globalThis.window, doc = globalThis.document,
    importModule = (url) => import(url), notice = (text, tone, options) => showToast(text, tone, options),
    now = () => Date.now(), mountTimeoutMs = WIDGET_REQUEST_TIMEOUT_MS } = {}) {
    const marker = bootMarker(win, now);
    const mounts = new Map();  // contribution key -> { item, controller, status, stage, handle }
    const providers = new Map();  // contribution key -> { entry, record }
    const providerListeners = new Set();
    const disposers = [];
    let mode = 'idle';  // idle → running | off (asked) | skipped (marker) → stopped (Panic, unload, destroy)
    let batches = 0;
    let batchKeys = [];

    const live = (rec) => mounts.get(rec.item.key) === rec && (rec.status === 'mounting' || rec.status === 'mounted');
    const navigate = (href) => win?.location?.replace?.(href);

    function notifyProviders() {
        for (const listener of [...providerListeners]) {
            try { listener(); } catch (error) { console.error('provider listener failed', error); }
        }
    }

    function letGo(handle) {
        try {
            const result = handle?.dispose?.();
            if (typeof result?.then === 'function') result.then(null, (error) => console.warn('page module dispose failed', error));
        } catch (error) { console.warn('page module dispose failed', error); }
    }

    function retire(rec) {
        if (mounts.get(rec.item.key) === rec) mounts.delete(rec.item.key);
        const handle = rec.handle;
        rec.status = 'retired';
        rec.handle = null;
        rec.controller.abort();
        let dropped = false;
        for (const [key, provider] of providers) {
            if (provider.record === rec) { providers.delete(key); dropped = true; }
        }
        if (dropped) notifyProviders();
        if (handle) letGo(handle);
    }

    function fail(rec, error) {
        if (!live(rec)) return;  // retired while starting: its end is not a fault
        retire(rec);
        const { item } = rec;
        const reason = String(error?.message || error || 'unknown error');
        store.markFault(item, reason);
        const skill = item.skill_label || item.skill;
        const name = item.label && item.label !== skill ? `${item.label} (${skill})` : skill;
        notice(fmt('{name} stopped working: {reason}. It stays off until you reload or update the skill.', { name, reason }),
            'warn', { ttl: 0 });
    }

    function registerProvider(rec, kind, impl) {
        const { item } = rec;
        if (item.provides !== kind || item.runtime !== 'client') {
            throw new Error(`${item.key} declares no client ${String(kind)} provider to register`);
        }
        if (!impl || typeof impl !== 'object') throw new TypeError('providers.register(kind, impl) needs the provider object');
        if (!live(rec)) return () => {};
        const entry = Object.freeze({ key: item.key, skill: item.skill, skill_label: item.skill_label, label: item.label,
            kind, destination: item.destination || '', languages: item.languages || [], impl });
        providers.set(item.key, { entry, record: rec });
        notifyProviders();
        return () => {
            if (providers.get(item.key)?.entry !== entry) return;
            providers.delete(item.key);
            notifyProviders();
        };
    }

    // Exactly these facts and capabilities; one a future version adds is absent here, never a silent stub.
    function contextFor(rec) {
        const { item, controller } = rec;
        const root = doc?.documentElement;
        return Object.freeze({
            api: HOST_MODULE_API,
            skill: item.skill,
            key: item.key,
            signal: controller.signal,
            providers: Object.freeze({ register: (kind, impl) => registerProvider(rec, kind, impl) }),
            notice(text) {
                if (live(rec)) notice(`${item.skill_label || item.skill}: ${boundedText(String(text ?? ''), 300)}`, 'info');
            },
            draft: Object.freeze({ insert: (text) => live(rec) && Boolean(draft?.insert(String(text ?? ''))) }),
            theme: Object.freeze({
                get dark() { return root?.dataset?.theme !== 'light'; },
                get reducedMotion() {
                    try { return Boolean(win?.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches); } catch { return false; }
                },
            }),
            origin: Object.freeze(originFacts(win)),
        });
    }

    function mount(item) {
        const rec = { item, controller: new AbortController(), status: 'mounting', stage: 'import', handle: null };
        mounts.set(item.key, rec);
        let timer = 0;
        const starting = Promise.resolve().then(async () => {
            const module = await importModule(item.render.url);
            rec.stage = 'mount';
            if (typeof module?.mount !== 'function') throw new Error('the module exports no mount(ctx)');
            return module.mount(contextFor(rec));
        });
        const bound = new Promise((_resolve, reject) => {
            timer = setTimeout(() => reject(new Error(tr('host_modules.timeout', 'it did not start in time'))), mountTimeoutMs);
        });
        return Promise.race([starting, bound]).then((handle) => {
            if (live(rec)) { rec.status = 'mounted'; rec.handle = handle || null; }
            else letGo(handle);  // retired while starting: let its late handle go at once
        }, async (error) => {
            starting.then(letGo, () => {});  // a start that outlives the bound is let go when it lands
            // A revision replaced since the read answers 404: read again, and the next revision mounts instead.
            if (rec.stage === 'import' && live(rec)) await store.refresh();
            fail(rec, error);
        }).finally(() => clearTimeout(timer));
    }

    function sync() {
        if (mode !== 'running') return;
        const wanted = new Map(store.hostEntries().map((item) => [item.key, item]));
        for (const rec of [...mounts.values()]) {
            const item = wanted.get(rec.item.key);
            if (!item || item.revision !== rec.item.revision || item.render.url !== rec.item.render.url) retire(rec);
        }
        const batch = [...wanted.values()].filter((item) => !mounts.has(item.key) && !store.fault(item.key));
        if (!batch.length) return;
        // The marker stands before the first import of every batch and goes once the whole batch has settled.
        batchKeys = [...new Set([...batchKeys, ...batch.map((item) => item.key)])];
        marker.write(batchKeys);
        batches += 1;
        Promise.allSettled(batch.map(mount)).then(() => {
            if (mode !== 'running') return;  // stop() already cleared it
            batches -= 1;
            if (!batches) { batchKeys = []; marker.clear(); }
        });
    }

    /** Panic, unload or teardown: every module is disposed and none mounts again in this page. */
    function stop() {
        if (mode === 'stopped') return;
        const wrote = mode === 'running';  // a skipped start keeps the marker it found: only Start with plugins clears it
        mode = 'stopped';
        for (const rec of [...mounts.values()]) retire(rec);
        batches = 0;
        batchKeys = [];
        if (wrote) marker.clear();  // an orderly end is not a start that did not finish
    }

    function restartWithPlugins() {
        marker.clear();
        navigate(pluginsUrl(String(win?.location?.href || ''), true));
    }

    function onDoorClick(event) {
        const door = event.target?.closest?.('[data-plugins="off"]');
        if (!door) return;
        event.preventDefault?.();
        navigate(pluginsUrl(String(win?.location?.href || ''), false));
    }

    function onPageHide(event) {
        if (!event?.persisted) stop();
    }

    const api = {
        /** Decide this start (run, asked off, or skipped after an unfinished start) and follow the store. */
        start() {
            if (mode !== 'idle') return api;
            if (pluginsOffRequested(String(win?.location?.href || ''))) {
                mode = 'off';
                notice(tr('host_modules.off', 'Started without plugins.'), 'info', { ttl: 0,
                    action: { label: tr('host_modules.restart_with', 'Restart with plugins'), run: restartWithPlugins } });
            } else {
                // Age is no proof the frozen module was fixed: a phone or mini app reopened hours later skips too.
                if (marker.read()) {
                    mode = 'skipped';
                    notice(tr('host_modules.skipped', 'Plugins were skipped because the last start did not finish.'), 'warn',
                        { ttl: 0, action: { label: tr('host_modules.start_with', 'Start with plugins'), run: restartWithPlugins } });
                } else {
                    mode = 'running';
                }
            }
            disposers.push(store.onChange(sync));
            win?.addEventListener?.('ouro:panic', stop);
            win?.addEventListener?.('pagehide', onPageHide);
            doc?.addEventListener?.('click', onDoorClick);
            disposers.push(() => {
                win?.removeEventListener?.('ouro:panic', stop);
                win?.removeEventListener?.('pagehide', onPageHide);
                doc?.removeEventListener?.('click', onDoorClick);
            });
            sync();
            return api;
        },
        /** `idle`, `running`, `off`, `skipped` or `stopped`. */
        mode: () => mode,
        /** Keys of the modules mounted now. */
        mounted: () => [...mounts.values()].filter((rec) => rec.status === 'mounted').map((rec) => rec.item.key),
        /** Providers page modules registered (`ctx.providers.register`), of one kind (`stt`, `tts`). */
        clientProviders: (kind) => [...providers.values()].map(({ entry }) => entry).filter((entry) => entry.kind === kind),
        onProvidersChange(listener) {
            providerListeners.add(listener);
            return () => providerListeners.delete(listener);
        },
        stop,
        destroy() {
            stop();
            for (const dispose of disposers.splice(0)) dispose();
            providerListeners.clear();
        },
    };
    return api;
}

let shared = null;

/** Start the page's one runtime (app.js, once); later calls return it. */
export function startHostModules(options) {
    if (!shared) shared = createHostModules(options).start();
    return shared;
}

/** The page's runtime, or null before app.js started it. */
export const getHostModules = () => shared;
