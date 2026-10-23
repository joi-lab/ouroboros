# Module widget host protocol

This document is for anyone writing an interface that runs reviewed `kind: "module"` widgets the way the builtin interface does. It is the host side: how to build the frame, the message grammar between the page and the frame, and when to start and stop a widget. The author side, the `window.OuroborosWidget` API a widget is written against, is in [`CREATING_SKILLS.md`](CREATING_SKILLS.md) ("`kind: "module"` widgets"). The rest of the client contract is in [`CLIENT_CONTRACT.md`](CLIENT_CONTRACT.md).

A widget written for Ouroboros only works in your interface if your host keeps every promise below. Nothing here requires importing the builtin interface. The builtin implementation is `web/modules/widget_module.js` (the page side) and `web/modules/widget_frame.js` (the bootstrap scripts injected into the frame); reading them is optional.

## 1. What the core gives you

- **Cards:** `GET /api/widgets` returns `ui_tabs`, one `WidgetTab` per card: `key`, `skill`, `tab_id`, `title`, `icon`, `ws_prefix`, `render`, `span`, `grid_span` and `revision`.
- **A module card's `render`:** `{kind: "module", entry, start, appearance?, height?, max_height?}`.
  - `entry` is the file name of the reviewed script. Build its URL from the name's letters, digits, `.`, `_` and `-` only, as the builtin host does.
  - `start` is `auto`, `manual` or `retain` (§7).
  - `appearance` (`host`, `independent` or `fixed`) records the author's intent only. It changes nothing in the host.
  - `height` is a fixed frame height (320 to 8192 pixels). Without it the frame sizes itself (§5).
  - `max_height` lowers the self-sizing ceiling (320 to 8192, default 8192).
- **The script:** `GET /api/extensions/{skill}/module/{entry}` answers the reviewed source as JavaScript text. It is never cached and any origin may load it. `409` means the skill has no live bundle; `404` means the file was not captured.
- **Change signals:**
  - `revision` is the skill's live content hash. A card whose `revision` changed while it runs is stopped (§6) and started again.
  - An `extension_lifecycle` WebSocket frame means "read `GET /api/widgets` again". A card that left the list is stopped.
- **The owner's launch choice:** `GET /api/ui/preferences` returns `widget_start_mode`, keyed `"<skill>:<tab_id>"`. It overrides the card's own `start`. `POST /api/ui/preferences` with `{widget_start_mode}` saves it.

Fetch the script with the owner's session, from the page. The builtin host gives up after 25 seconds and shows a retryable error.

## 2. Building the frame

Create one `<iframe>` per running card with exactly these attributes:

| Attribute | Value |
|---|---|
| `sandbox` | `allow-scripts allow-pointer-lock allow-downloads` |
| `allow` | `autoplay; fullscreen; clipboard-write` |
| `allowfullscreen` | present |

Never add `allow-same-origin`: the frame stays an opaque origin with no direct access to the page's cookies, storage or DOM. Cookie attachment to asset requests is browser-dependent; use the host fetch bridge, not a promise that passive requests are always anonymous. Never add top navigation, popups, forms or modals.

Set the frame's `srcdoc` property (not an attribute string) to this document, in this order:

```html
<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="POLICY"></head><body><div id="root"></div><script>BRIDGE</script><script>RESIZE</script><script>MODULE</script></body></html>
```

- **`POLICY`** is the document policy below, with `ORIGIN` the page's own origin (for example `https://ouro.example`) and `SKILL` the URL-encoded skill name. An opaque frame's `'self'` matches nothing, so the sources are absolute.

  ```
  default-src 'none'; script-src 'unsafe-inline' 'wasm-unsafe-eval' blob: ORIGIN/api/extensions/SKILL/module/; worker-src blob:; style-src 'unsafe-inline'; img-src data: blob: ORIGIN/api/extensions/SKILL/; media-src data: blob: ORIGIN/api/extensions/SKILL/; font-src data: blob: ORIGIN/api/extensions/SKILL/
  ```

- **`BRIDGE`** is your bootstrap (§3). It must run before the module.
- **`RESIZE`** is your size reporter (§5), or empty when the card has a fixed `height`.
- **`MODULE`** is the script text, with every `</script` written as `<\/script` and every `<!--` as `<\!--`. It runs as a classic script, even when the file is named `.mjs`.

## 3. The bootstrap inside the frame

Before the module runs, the bootstrap must provide what `CREATING_SKILLS.md` promises the author:

- `window.OuroborosWidget = {fetch, onEvent, onTheme, download, openExternal}`;
- `window.fetch` replaced by the same bridged `fetch`;
- `window.__ouroWidgetOnDispose(fn)`, which registers a dispose hook (a hook registered while disposing runs at once);
- `window.open(url)` routed to `openExternal`, returning `null`;
- a click on an `<a download>` whose `href` is a frame-created `blob:` URL, a `data:` URL or a URL under the skill's prefix becomes `download`;
- a trusted click on any other `<a href>` outside the skill's prefix becomes `openExternal`;
- a fault channel: listeners on `window` for `error`, `unhandledrejection` and `securitypolicyviolation` post `ouro-widget-error`. Post at most 10 faults per frame, and each distinct kind and message once.

Every message the bootstrap posts goes to `window.parent` with target origin `'*'` and carries the mount's `nonce` (§4). It accepts only messages whose `event.source` is `window.parent` and whose `nonce` matches.

`openExternal(url)` accepts only `http:`, `https:` and `mailto:` URLs and needs a user action in the frame. The frame's own `Blob` URLs cannot be fetched by the page, so the bootstrap remembers the `Blob` behind each URL it creates and sends the `Blob` itself.

## 4. The message grammar

Generate a fresh random `nonce` string for every mount and embed it in the bootstrap. Every message in both directions is a plain object with a `type` and that `nonce`. The page accepts a message only when `event.source` is this frame's `contentWindow` and the `nonce` matches. It answers with `frame.contentWindow.postMessage(message, '*')`.

**Frame to page:**

| `type` | Fields | Meaning |
|---|---|---|
| `ouro-widget-fetch` | `id`, `url`, `init: {method, headers, body, timeoutMs}` | Start one bridged request (§5). `id` is unique within the frame; `headers` is a list of `[name, value]` pairs. |
| `ouro-widget-fetch-pull` | `id` | The frame wants the next body chunk. |
| `ouro-widget-fetch-abort` | `id` | Abort that request. |
| `ouro-widget-events` | `op: "subscribe" \| "unsubscribe"` | Start or stop forwarding this skill's WebSocket events. |
| `ouro-widget-theme` | `op: "subscribe" \| "unsubscribe"` | Start or stop sending the theme. |
| `ouro-widget-download` | `id`, `name`, `source` | Save a `Blob`, a `data:` URL or a URL under the skill's prefix. |
| `ouro-widget-open-external` | `id`, `url` | Open an external link. |
| `ouro-widget-resize` | `height` | The measured content height in pixels (§5). |
| `ouro-widget-error` | `kind: "error" \| "rejection" \| "csp"`, `message`, `source`, `line` | A fault inside the frame. Show it on the card; builtin logging is described in §5. |
| `ouro-widget-disposed` | | The frame finished its dispose hooks (§6). |

`init.body` is a structured-clone value: `null`, a string, an `ArrayBuffer`, a typed-array view or a `Blob`. Preserve a view's offset and length. There is no base64 wrapper or required transfer list for request buffers. The builtin bridge posts the body without a transfer list; the page passes that cloned value to fetch. Response chunks have the separate transfer behavior below.

**Page to frame:**

| `type` | Fields | Meaning |
|---|---|---|
| `ouro-widget-fetch-chunk` | `id`, `phase`, and per phase: `headers`: `status`, `statusText`, `headers` (`[name, value]` pairs); `data`: `chunk` (an `ArrayBuffer`, transferred); `end`; `error`: `error` (text) | One step of a bridged response. |
| `ouro-widget-event` | `event`, `data` | One of the skill's WebSocket events. |
| `ouro-widget-theme` | `theme: "light" \| "dark"` | The page's current theme. |
| `ouro-widget-download-result` | `id`, `result` | The outcome of a download. |
| `ouro-widget-open-external-result` | `id`, `result` | The outcome of opening a link. |
| `ouro-widget-dispose` | | Stop now (§6). |

## 5. What the page does for each message

**Fetch:**
1. Resolve `url` against the page's origin. Refuse, with the `error` text `module widget fetch outside extension route prefix`, unless the result has the page's origin and its path starts with `/api/extensions/<skill>/` for this card's skill.
2. Send the request from the page with the owner's session (`credentials: "same-origin"`), the upper-cased method, the headers and the body. Use `redirect: "error"`: the prefix is checked once, so a followed redirect could carry the request and the session anywhere.
3. Post `headers` with the status, status text and every response header.
4. Read the body one chunk per `ouro-widget-fetch-pull`. Post each chunk as a `data` phase with its bytes as a transferred `ArrayBuffer`. Post `end` when the body ends. A `HEAD` request and a 204, 205 or 304 answer have no body.
5. On any failure post `error`.

There is no default timeout. A positive `init.timeoutMs` aborts the request after that many milliseconds, with the error text `widget request timed out`. `ouro-widget-fetch-abort` and stopping the card (§6) abort it too.

**Events.** While the frame is subscribed, forward every WebSocket frame whose `type` starts with this card's `ws_prefix` as `{type: "ouro-widget-event", event: <type without the prefix>, data: <the frame's data, or {}>}`. Other skills' frames never reach the frame.

**Theme.** On `subscribe`, post the current theme at once and again whenever it changes. `unsubscribe` stops that.

**Size.** Only a card without a fixed `height` sizes itself. The frame starts at 320 pixels. On `ouro-widget-resize`, set the frame's height to `min(ceiling, max(320, height + 2))`, where `ceiling` is `max_height` (default 8192) and the 2 pixels are the frame border. Ignore a height that is not a positive number. With a fixed `height`, ignore these messages. The builtin size reporter measures `#root` with a `ResizeObserver`, reports in the next animation frame, posts only a changed integer height, and hides the document's vertical scrollbar below the ceiling.

**Downloads.** A `Blob` or a `data:` URL is saved as it is. A URL must have the page's origin and lie under the skill's prefix; anything else fails. Answer with `result: {ok: true, ...}` or `{ok: false, error}`.

**External links.** Open the link only while the page has a user activation. Answer with `result: {ok: true, ...}`, `{ok: false, degraded, ...}` when only a fallback (such as copying the link) was possible, or `{ok: false, error}`. The frame treats `ok` or `degraded` as success.

**Faults.** Show the fault in the card's status (a card in a message shows it there) and leave the card running. The builtin host additionally posts it once per kind and message, at most 10 per mount, to `POST /api/widgets/fault` (`WidgetFaultRequest`): this card's own `skill`, `tab_id` and `revision`, `where: "board"` or `"message"`, and the frame's `kind`, `message` (clipped to 500 characters), `source` (200) and `line`; a value that is not a string is sent as `""`, a line that is not a whole number from 0 as `0`. Never take who the widget is from the message, and count per mount, not per frame: the widget's own code can read the nonce and post directly. Reporting is optional for other clients. A report is best effort: no retry, and a refusal changes nothing on the card. Until the stop finishes (§6) the frame still reports.

The server normalizes unpaired UTF-16 surrogates in the clipped text to U+FFFD before writing UTF-8; valid emoji remain intact.

## 6. Stopping a widget

Stopping is a handshake:

1. Post `ouro-widget-dispose`.
2. Keep answering bridged requests: the widget's dispose hooks may save state through its own routes.
3. When `ouro-widget-disposed` arrives, or after 1000 milliseconds, whichever is first: abort every pending request, stop forwarding events and the theme, stop listening for the frame's messages and remove the frame.

A frame that has already gone away is removed at once.

## 7. When widgets run

| `start` | Behaviour |
|---|---|
| `auto` | Starts when the widgets view is shown and stops when the owner leaves it. |
| `manual` | Shows the title, icon and a Start button. Runs only after Start. Leaving the view stops it. |
| `retain` | Starts on the first visit to the widgets view and keeps running on other views until Stop, until the skill leaves the card list, or until the page goes away. |

The owner's choice in `widget_start_mode` overrides `start`. Stop always wins: the builtin host keeps the owner's Stop on that device across reloads until Start or a change to `auto` / `retain`. Installing or enabling a skill never starts a widget: only showing the widgets view does. A frame never outlives the page that hosts it.

On WebKit, Android WebView and Firefox a frame shares the page's thread, so a start that loops or blows up takes the page with it. The builtin host writes each frame's start to a local ledger before the frame gets its document and clears it one second after the frame's first `load`, or when the page hides (its thread outlived the start); a card whose last start with this revision the page did not outlive is not started by itself on any view (its facade, one line, Start). A page opened with `?hold=all` holds every frame for that load.

## 8. Other widget kinds

- **`kind: "iframe"`:** a frame whose `src` is `/api/extensions/<skill>/<render.route>`, with the same `sandbox` and `allow` attributes, no bridge and no document policy. Remove it at once on stop.
- **`kind: "declarative"`:** a component description the host draws itself. The components are listed in `CREATING_SKILLS.md`. An interface that does not draw them should say the card is not supported rather than guess.

## 9. Frames in a message

Ouroboros can show a widget, or a small app of its own, inside its own chat message: a living card ([`CLIENT_CONTRACT.md`](CLIENT_CONTRACT.md) §5 "Living cards"). A frame in a message is not a Widgets card. It never joins the widgets view's cards or launch policy, and leaving the widgets view stops nothing in a message; the message disposes its frames when its row goes, and with the page. The builtin implementation is `web/modules/card_frames.js` and `web/modules/chat_cards.js`.

- **Size.** A frame in a message is at least 120 pixels tall and at most 70 % of the window's height. Taller content is cut until the owner presses Expand (then up to the 8192-pixel ceiling); Collapse cuts it again. There is no 320-pixel floor. A module widget's `height` is honoured up to the cap; a self-sizing one reports through `ouro-widget-resize` as on the widgets view. A declarative widget shown in a message is measured and cut the same way. The builtin host runs at most three frames in one feed, started when they come near the view; a frame far from it rests as the card's words with Open. A running frame has Close, which rests it the same way on that device until Open; a frame whose last start the page did not outlive (§7) rests so too, with its line, and an app opened from that hold starts without its saved state.
- **A skill's widget in a message** is the tab `GET /api/widgets` lists for the card's `{skill, tab_id}`. When it is not listed, show the card's words and say that the skill is off. The module bridge, its events and the theme work as on the widgets view; a widget fault is shown on the card. The card's `input` (an object) gives a declarative widget's form fields of the same names their first values; what the owner typed in this page session wins. A module widget gets no input.
- **An app** is HTML Ouroboros wrote, read as text from `GET /api/cards/{card_id}/app` and never opened as a page. It runs in its own frame, not the module frame: a created `<iframe>` with `sandbox="allow-scripts"` and nothing else (no `allow` attribute), whose `srcdoc` property is set to `<!doctype html><html data-theme="light|dark"><head>`, then this document policy as the head's first element, then a viewport line, the theme and the bridge script, then `</head><body>`, the app's HTML and `</body></html>`:

  ```
  default-src 'none'; script-src 'unsafe-inline' blob: data:; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:
  ```

  An app has no fetch, WebSocket or load, no skill route, no request relay, no events, no downloads and no links out. No page policy covers two paths to the network: navigating its own frame (every navigation is a request; when one loads a second document the host removes the frame and says so on the card, while a navigation answered without a document leaves the app running) and WebRTC. The frame keeps the app away from Ouroboros and the page, not from the network; its only author is Ouroboros, which has the network anyway. The theme reaches the app as `data-theme` and the custom properties `--ouro-text`, `--ouro-text-meta`, `--ouro-accent`, `--ouro-surface`, `--ouro-tray`, `--ouro-border`, `--ouro-ok`, `--ouro-warn`, `--ouro-error`, `--ouro-font` and `--ouro-radius`, read where the frame lies: inside a dark voice they are that voice's own inks and surfaces, at mount and at every theme change.
- **The app bridge** is four messages from the frame and one from the page, each carrying the mount's nonce; the page acts only on messages from that frame's own window:
  - `ouro-card-resize {height}`: the content height. The bridge reports it itself as the document grows.
  - `ouro-card-error {kind, message}`: a script error, an unhandled rejection or a policy refusal (`kind` `error`, `rejection` or `csp`), sent by the bridge itself, each once and ten at most; the host shows it on the card.
  - `ouro-card-state {state}`: JSON text of at most 32 KiB. The host keeps it per card and app bytes in the tab's session storage and hands it back at the next mount.
  - `ouro-card-submit {value, summary}`: the host puts one line into the owner's draft, `From «<title>»: <summary>` followed by the value as compact JSON when that is 300 characters or fewer, and focuses the field. It never sends: only the owner's Send does.
  - `ouro-card-theme {theme: {name, css}}`, page to frame: the theme changed.

  Inside the frame an app uses `window.OuroborosCard = {resize(height), state: {value, save(value)}, submit(value, summary), theme, onTheme(callback)}`.
