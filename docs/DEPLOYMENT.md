# DEPLOYMENT.md — Deployment Notes

## Roots, Ports and One Server per Data Directory

A deployment places Ouroboros with the existing environment keys, read at start:
`OUROBOROS_REPO_DIR` (the body checkout), `OUROBOROS_DATA_DIR` (memory, state,
settings, logs), `OUROBOROS_SETTINGS_PATH`, `OUROBOROS_PID_FILE` (the launcher's
single-instance lock), `OUROBOROS_PORT_FILE` (where the main server publishes its
bound port) and `OUROBOROS_APP_ROOT` (the parent of the defaults,
`~/Ouroboros`). Work roots take a saved setting first, then the environment,
then the `~/Ouroboros` default: `OUROBOROS_SUBAGENT_PROJECTS_ROOT`,
`OUROBOROS_SUBAGENT_WORKTREE_ROOT`, `OUROBOROS_DELIVERABLES_ROOT`. Compose,
systemd or a shell may load these from an env file; Ouroboros reads no env file of
its own. A direct `server.py` without `OUROBOROS_REPO_DIR` serves the checkout it
runs from. Set `OUROBOROS_DATA_DIR` explicitly in every new recipe: with only
`OUROBOROS_APP_ROOT`, a direct server keeps its historical `~/Ouroboros/data`
(ARCHITECTURE §7).

`OUROBOROS_SERVER_PORT` is the requested main port for a direct server and for
the launcher (default 8765); when it is taken the server falls back to the next
free port, skipping this installation's Host Service and local-model ports, and
the source CLI, Settings, extensions and the launcher read the port it actually
published. The packaged desktop CLI deliberately uses `~/Ouroboros/data` and
scrubs path overrides (including `OUROBOROS_PORT_FILE`) before starting its
inner CLI; select a relocated server with `--url` or `OUROBOROS_URL`. A container publishes a fixed port: keep the request free inside it.

Exactly one server runs on one data directory. Each server holds a kernel lock on
`<data>/state/server.lock` for its lifetime, whatever repository, PID file or
port it uses; a second one waits briefly (a Restart successor) and then exits 2
with the reason, and a filesystem without kernel file locks refuses the start.
A server from a release before this lock is not detected by it: stop it first.

## Containers

Two images, one recipe file: [`packaging/docker/compose.yaml`](../packaging/docker/compose.yaml)
is the executable recipe for both, built from the repository root. Run one
profile per volume set; never point two containers at one data volume.

| | Immutable image (`ouroboros-web`) | Managed body (`ouroboros-managed`) |
|---|---|---|
| Code and packages | the image's `/app` | a checkout and runtime venv on a volume |
| Update | build and deploy a new image | Settings → Update, as on the desktop |
| Entry | `python server.py` | the common launcher, `--no-ui` |
| Volumes | data, work, `HOME` | one parent volume, or body/data/work/home |

**Exposure.** Both images bind `0.0.0.0` and set no `OUROBOROS_NETWORK_PASSWORD`:
a missing password only warns and the gate then admits every request, so a
published port without it exposes the whole owner surface. The recipes publish on
`127.0.0.1`; set the password before publishing further, or keep the port
unpublished. Packaging adds no stronger boundary of its own.

**Managed body.** `docker build --target managed --build-context
ouroboros-seed=<dir> .` takes a released seed, the `repo.bundle` and
`repo_bundle_manifest.json` that `scripts/build_repo_bundle.py` emits from a clean
annotated release tag; the build refuses a bundle without complete history. The
image keeps only the launcher; its layout puts the body at
`/ouroboros/body/{repo,venv}`, data at `/ouroboros/data`, work at
`/ouroboros/work` and `HOME` at `/ouroboros/home`. First boot clones the seed into
the body and prepares `--runtime-venv` (`ouroboros/runtime_venv.py`): it needs
network for the dependency lock, and an interrupted preparation resumes on the
next start. Later boots, Update, self-modification and Restart run on the volume;
removing and recreating the container keeps body, packages, data, work and
`HOME`. Mount volumes at `/ouroboros` or at the four kind directories, never at
the repository itself: bootstrap refuses a mount point without a checkout and
moves nothing. A new image changes only the launcher and the seed for future first
boots; a new Python, system package or browser build still needs a new image, and
a venv created by another Python is refused untouched with the recovery step
(move it aside; the next start prepares a fresh one). Git rollback restores code
and its lock, not data or the exact package set.

**Restart policy and Panic.** Use `restart: on-failure` with automatic intent
(both recipes): after Panic the automatic entry exits 0, leaving the stop marker
intact, so the container stays stopped; Panic itself exits 99 in a direct server
and 0 in the launcher. `always` and `unless-stopped` keep restarting clean exits;
without automatic intent a restart policy consumes the marker. The recipes show
the foreground owner resume (`--launch-intent owner`, or
`OUROBOROS_LAUNCH_INTENT=owner` for the immutable image); an ordinary `docker
start` keeps automatic intent, and deleting the marker is never the resume. A
refused start (a held data directory, a mounted repository, a foreign venv) exits
nonzero, so `on-failure` retries it with Docker's growing delay until the cause is
fixed; a second managed container sharing the data volume loses the launcher lock
and exits 0. `init: true` reaps orphans the launcher or server cannot. See
Docker's [restart policy documentation](https://docs.docker.com/engine/containers/start-containers-automatically/).

## Trusted Docker / Kubernetes Non-Local Binds

By default, saving `OUROBOROS_SERVER_HOST=0.0.0.0` through the Settings UI
requires `OUROBOROS_NETWORK_PASSWORD` in the same save. This keeps desktop and
local-network launches from accidentally exposing the full Ouroboros HTTP and
WebSocket surface without the built-in password gate.

Trusted container deployments may opt out with:

```bash
OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1
```

Use this flag only when access is already restricted by external
infrastructure, for example:

- ingress authentication
- VPN-only routing
- private Kubernetes service/network policy
- an authenticated reverse proxy

With the flag enabled, Ouroboros still warns when saving a non-localhost bind
without `OUROBOROS_NETWORK_PASSWORD`, but the Settings UI no longer blocks
ordinary settings saves such as API-key updates. Do not use this flag on an
open LAN or public port.

## The Main Interface and a Separately Hosted One

`OUROBOROS_UI_DIR` chooses what the core serves at `/` and `/static/*`:
empty for the builtin interface, `none` for no main page, or an absolute (or `~`)
directory with its own `index.html`, laid out like `web/`. It applies when the
server starts, so a saved change waits for a restart. A saved value, an empty one
included, beats the environment variable; the environment counts only while the
setting was never saved, and Ouroboros never copies it into `settings.json`. To
return to the builtin interface, clear the setting (in Settings → Advanced, or in
`settings.json` while Ouroboros is stopped), restart and reopen the address.
`GET /api/state` reports what is actually served as `served_ui`.

- A directory is trusted, not sandboxed: every file in it, dot-files included, is
  published under `/static/`, so point it at a dedicated build output. Its HTML is
  served unchanged; a custom interface used as a Telegram Mini App includes
  Telegram's script itself.
- A directory that is relative, missing, has no `index.html` or contains the
  core's data, source or settings tree is not applied: the builtin interface
  serves and `served_ui.error` says why.
- A selected directory may also supply complete `onboarding.html` and
  `auth/index.html` pages. The login's `auth/` assets are public before sign-in;
  publish only that page's public build files there. Missing optional pages
  fall back whole to builtin. `/onboarding?builtin=1` and
  `/auth/login?builtin=1` keep builtin recovery without changing the setting.
  API writers and password checks remain the core's (§2–4 of CLIENT_CONTRACT).
- With `none`, `/` answers a 404 page and `/static/*` 404; windows that open the
  core's own address (desktop, Android, Telegram) show that page too. The API,
  the WebSocket, sign-in, the setup wizard (`/onboarding`, with its own assets
  under `/onboarding/static/`), extension routes and files keep working.

An interface hosted elsewhere must share one browser origin with the core:
cross-origin access and mounting under a sub-path are not supported. A reverse
proxy sends the core's prefixes (`core_paths` in `GET /api/contract`: `/api`,
`/auth`, `/onboarding`, `/ws`) to the core and everything else to the interface
host, passes WebSocket upgrades, keeps request bodies and event streams
unbuffered and never answers a core path with the interface's page. Client rules
are in [`CLIENT_CONTRACT.md`](CLIENT_CONTRACT.md); a verified Caddy setup with
its coverage limits is in [`examples/separate_ui/`](examples/separate_ui/).

The password gate asks only non-loopback clients, and behind a proxy the client
is what the proxy says. uvicorn takes `X-Forwarded-For` and `X-Forwarded-Proto`
only from the addresses in `FORWARDED_ALLOW_IPS` (default `127.0.0.1`), using
the last `X-Forwarded-For` entry that is not such a proxy:

- A proxy on the core's machine must set `X-Forwarded-For` to the peer it saw.
  Without the header every visitor reaches the core as loopback, and a proxy that
  passes a visitor's own header through unchanged lets that visitor claim
  loopback; either way the password is skipped. A browser on the proxy's machine
  is loopback by design.
- A proxy on another machine must be listed in `FORWARDED_ALLOW_IPS`; otherwise
  the core treats the proxy itself as the client and does not see TLS, so the
  session cookie is not marked `Secure`.
- Visitors who are not loopback need `OUROBOROS_FILE_BROWSER_DEFAULT` for Files;
  without it the path-addressed `/api/files/*` operations refuse them.

## Extra CA Certificates

Ouroboros verifies its own provider calls (OpenRouter, OpenAI-compatible endpoints,
Anthropic, GigaChat, model catalogs and Provider Test) against the `certifi`
bundle, never the operating-system store. A deployment behind a TLS-inspecting
proxy, or one that talks to an endpoint signed by a CA `certifi` lacks (for
example the Russian Trusted Root CA behind GigaChat), sets
`OUROBOROS_EXTRA_CA_BUNDLE` to a PEM file holding the missing CA certificates.
The file is added on top of the defaults, so every other provider keeps working;
a path that cannot be read fails the call loudly instead of silently falling
back to the defaults. In Docker, mount the file and pass the setting:

```bash
docker run --rm -p 8765:8765 \
  -v "$PWD/extra-ca.pem:/certs/extra-ca.pem:ro" \
  -e OUROBOROS_EXTRA_CA_BUNDLE=/certs/extra-ca.pem \
  ouroboros-web
```

On a desktop install the same key lives in Settings → Advanced. It covers the
provider calls: model requests, model catalogs, Provider Test, pricing and
capability probes. Everything else keeps its own trust store: `git`, `uv`,
`pip`, the Claudexor engine and its runtime downloads, the Telegram skill, MCP
servers, the web-search scraper and the browsers.

## Logs and External Monitoring

Ouroboros sends no telemetry; nothing below leaves the machine until the
deployment connects a destination of its own.

- The server process writes `logs/server.log` (2 MB × 4) and its stderr. Each
  pool worker logs to its stderr only: the desktop launcher copies it into
  `logs/agent_stdout.log`, Docker keeps it as the container log.
- The server and every pool worker configure this logging at their real
  start (`ouroboros/process_logging.py`), whatever the entry point (`python
  server.py`, `ouroboros server`, Colab); the desktop launcher keeps its own
  `logs/launcher.log` and routes its uncaught exceptions there. Under
  `OUROBOROS_WORKER_START_METHOD=fork` a worker inherits the server's handlers
  instead (`docs/PERSISTENCE.md`). Not covered: whatever runs before that call
  (the entry module's own imports), out-of-process extension children, the
  startup historical-audit child, the local-model server and other helper
  processes. An unexpected failure — a task
  exception, a worker crash, an unhandled gateway request (HTTP 500), an
  uncaught thread exception — is a stdlib `logging` record with its traceback,
  so a handler attached in that process receives it. Message text passes the
  secret-redacting filter; tracebacks and structured extras do not.
- File collectors (OpenTelemetry Collector, Vector, Fluent Bit) can ship durable
  JSONL under `logs/` and task drives without changing Ouroboros (`docs/PERSISTENCE.md`).
  The record passport, `ouroboros/contracts/record_contract.py`, defines guaranteed
  anchors/fields, correlation, content, rotation, tools replicas, child-drive retention
  (`OUROBOROS_GC_RETENTION_DAYS`) and money's source: accounting views, never summed
  `llm_usage`. Its `LEGACY_RULE` explains v2 nullable tokens: retain the stamp and nulls;
  old zeros stay ambiguous. `docs/examples/log_collector/` is a verified metadata-only
  Vector setup.
- An error tracker attaches from inside the process: an in-process extension
  skill loads in the server and in every worker, and can initialise a
  Sentry-compatible SDK (self-hosted Sentry, GlitchTip) installed into
  Ouroboros's own environment (in Docker, a layer of the image; a skill-declared
  dependency would move the extension out of process, where it cannot see these
  records). Turn frame-local capture off and pass events through
  `ouroboros.observability.redact_projection` before they leave;
  `docs/examples/error_tracker/` is a verified extension that does this and
  says what still leaves (messages and exception text can quote task text).
  Failures that happen before the extension loads stay in the local logs only,
  as do the launcher's and the Claudexor daemon's.
