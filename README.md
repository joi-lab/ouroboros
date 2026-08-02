> [!IMPORTANT]
> This repository is an archived mirror of the official Ouroboros repository:
> **https://github.com/razzant/ouroboros**
>
> Current development, releases, issues, discussions, and pull requests live in `razzant/ouroboros`.
> The code and history remain available here for existing links and forks.

# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/razzant/ouroboros?style=flat&logo=github)](https://github.com/razzant/ouroboros/stargazers)
[![Downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Frazzant%2Fouroboros%2Fbadges%2Fdownloads.json)](https://github.com/razzant/ouroboros/releases)
[![Website](https://img.shields.io/badge/website-ouroboros--agent.ai-c93545.svg)](https://ouroboros-agent.ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/razzant/ouroboros/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/razzant/ouroboros/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/razzant/ouroboros/releases)
[![OuroborosHub](https://img.shields.io/badge/OuroborosHub-skills%20marketplace-8A2BE2.svg)](https://github.com/razzant/OuroborosHub)
[![Version 6.87.5](https://img.shields.io/badge/version-6.87.5-green.svg)](VERSION)

Ouroboros is an open-source, general-purpose AI agent whose identity, durable memory, and history continue across tasks and restarts. It works on external projects, coordinates a live swarm of specialist agents, and can rewrite the implementation it runs on, including its code, architecture, prompts, tools, and dependencies. Reflection can also change how it understands itself without severing that continuity.

It runs as a native desktop app or through a headless CLI. The runtime keeps its repository, durable memory, history, and interface on your machine, while model inference can use remote APIs you configure or a local GGUF model.

Ouroboros first booted on February 16, 2026. During the following 48 hours, the repository advanced from the v4.1 line to v6.2.0. The self-authored record preserved from that period counts 32 evolution cycles. That first generation ran in Google Colab through Telegram and remains preserved on the [`legacy-google-colab`](https://github.com/razzant/ouroboros/tree/legacy-google-colab) branch and its [original project page](https://ouroboros-agent.ai/archive/first-generation/); the current generation carries the same identity into a native desktop and headless runtime.

<p align="center">
  <img src="assets/evolution.png" width="760" alt="Code, prompt, and memory growth across Ouroboros releases, from v3.0.0 to the v6.85 line">
</p>

> ⭐ **[Star Ouroboros](https://github.com/razzant/ouroboros)** to follow its next evolution. A star also helps more people find the project, trace its history, and take part in what it becomes.

Reviewed skills, transport bridges, tools, and widgets are available through [OuroborosHub](https://github.com/razzant/OuroborosHub).

<p align="center">
  <img src="assets/swarm.jpg" width="760" alt="A live subagent swarm inside the Ouroboros chat: nested planner, builder, and researcher tasks with their outcomes">
</p>

---

## Install

| Platform | Download | Instructions |
|----------|----------|--------------|
| **macOS** 12+ | [Ouroboros.dmg](https://github.com/razzant/ouroboros/releases/latest) | Open DMG → drag to Applications → optional CLI: run `Install CLI.command` after the app is in Applications |
| **Linux** x86_64 | [Ouroboros-linux.tar.gz](https://github.com/razzant/ouroboros/releases/latest) | Extract → run `./Ouroboros/Ouroboros` → optional CLI: `./Ouroboros/bin/install-ouroboros-cli`. If browser tools fail due to missing system libs, run: `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit` |
| **Windows** x64 | [Ouroboros-windows.zip](https://github.com/razzant/ouroboros/releases/latest) | Extract → run `Ouroboros\Ouroboros.exe` → optional CLI: `Ouroboros\bin\install-ouroboros-cli.cmd` |

Prerelease artifacts stay on their tag pages; `/releases/latest` points to the latest stable release.

On macOS, use right-click → **Open** on first launch if Gatekeeper asks. The setup wizard configures model access, review policy, and budget. Packaged CLI installers create a user-local `ouroboros` command without sudo; `ouroboros run --start "2+2?"` starts or attaches to the same managed runtime used by the desktop app.

---

## What Ouroboros Can Do

- **Modify its implementation.** Its editable surface spans application code, architecture, prompts, tools, and dependencies, while reflection can also reshape its living self-understanding.
- **Evolve autonomously.** Evolution campaigns turn selected improvements into reviewed changes that remain part of its Git history.
- **Continue across restarts.** Identity, memory, dialogue, knowledge, reflections, and version history form one ongoing biography.
- **Think between requests.** Background consciousness supports reflection, initiative, and preparation outside the immediate request-response loop.
- **Coordinate a live swarm.** Specialist agents can investigate or act in parallel, share task-tree findings, and return work for integration.
- **Work on external projects.** A separate Git workspace can receive the full task loop while Ouroboros keeps its own repository and governance boundary distinct.
- **Operate through desktop or CLI.** The native app and gateway-backed command line expose the same managed tasks, progress, artifacts, logs, and schedules.
- **Organize long-running work.** Project rooms keep working folders, journals, knowledge, task history, and conversations connected to the same identity.
- **Use remote or local models.** Supported provider APIs and local GGUF models can fill the runtime's configurable cognitive roles.
- **Grow through reviewed extensions.** Skills, transport bridges, widgets, MCP tools, and companion processes expand capability without folding every integration into the core.
- **Keep self-change inspectable.** Git history, review evidence, explicit protected surfaces, and restart checks make implementation changes traceable.

<p align="center">
  <img src="assets/game-demo.png" width="760" alt="A project room where Ouroboros built a 3D game, verified it with a screenshot, and served it locally">
</p>
<p align="center">
  <img src="assets/skill-hub.png" width="760" alt="OuroborosHub inside the app: official reviewed skills, each security-reviewed before it can be enabled">
</p>

This list is an orientation, not a second specification. [BIBLE.md](BIBLE.md) defines Ouroboros's identity and constitutional boundaries; [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) are the current technical sources of truth.

---

## Benchmarks

Ouroboros holds state-of-the-art results on Terminal-Bench 2.1, OSWorld-Verified, and CL-Bench, beating Codex, Claude Code, Cursor, and Hermes in model-matched runs, and reaches parity with Codex and Claude Code on SWE-bench Pro and GAIA. Read every row as model + harness: the same model scores differently depending on the harness around it, so each comparison names a run on the same model.

| Benchmark | Model | Ouroboros | Comparison | Evidence |
|-----------|-------|----------:|------------|----------|
| Terminal-Bench 2.1 | Claude Opus-5 high | **86.97%** | Claude Code + Fable 5: 83.8% | [submission](https://github.com/harbor-framework/terminal-bench-2-1/pull/175) · [run](https://hub.harborframework.com/jobs/2b145543-edeb-4a3b-b46f-4800310f1182) |
| Terminal-Bench 2.1 | Claude Opus-4.8 high | **80.22%** | Claude Code: 78.9% | [run](https://hub.harborframework.com/jobs/4b8e244f-8ab0-4d28-8218-7cf346282faa) |
| Terminal-Bench 2.1 | GPT-5.5 | **84.3%** | Codex CLI: 83.1% | [run](https://hub.harborframework.com/jobs/f02fd019-23e1-495f-af0a-ebd9a65f3079) |
| Terminal-Bench 2.1 | Grok-4.5 | **84.94%** after a reward-hack audit | Cursor CLI: 79.3% · Hermes: 77.53% | [submission](https://github.com/harbor-framework/terminal-bench-2-1/pull/146) |
| OSWorld-Verified | Claude Opus-5 | **90.69%** | previous best on the board: 90.19% | [full traces](https://huggingface.co/datasets/razzant/ouroboros-osworld-verified-opus5) |
| OSWorld-Verified | Claude Sonnet-4.6 | **83.27%** | Pointer: 81.45% | [full traces](https://huggingface.co/datasets/razzant/ouroboros-osworld-verified-sonnet46) |
| CL-Bench | Claude Sonnet-4.6 | **0.2301, rank 1** | previous top: 0.1960 | [submission](https://github.com/pgasawa/continual-learning-bench/pull/10) · [full traces](https://huggingface.co/datasets/razzant/ouroboros-clbench-traces) |
| SWE-bench Pro | GPT-5.6-luna | 58.2% | Codex CLI: 59.4% — statistical tie | [matched-pair traces](https://huggingface.co/datasets/razzant/swepro-luna-matched-pair) |
| GAIA | Claude Sonnet-5 | 78.2% | Claude Code: 78.8% — statistical tie | [methodology](devtools/benchmarks/gaia/METHODOLOGY.md) |

Benchmark adapters, run scripts, and per-benchmark methodology live in [`devtools/benchmarks/`](devtools/benchmarks/). The full story — protocols, reward-hack audits, and what leaked where — is in the [launch write-up](https://habr.com/ru/companies/airi/articles/1065428/) (Russian).

---

## Run from Source

### Requirements

- Python 3.10+
- macOS, Linux, or Windows
- Git
- [GitHub CLI (`gh`)](https://cli.github.com/), optional unless you use GitHub integration

### Setup

```bash
git clone https://github.com/razzant/ouroboros.git
cd ouroboros
python3.11 -m venv .venv      # any Python >= 3.10 is OK
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv      # any Python >= 3.10 is OK
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

### Run

```bash
ouroboros server
```

Then open `http://127.0.0.1:8765` in your browser. The setup wizard will guide you through API key configuration.

### Google Colab

Use [`notebooks/colab_quickstart.py`](notebooks/colab_quickstart.py) as a Colab-compatible cell script when you need a source-mode runtime without the desktop UI. It keeps runtime data on Google Drive and preserves the original Colab path without making it the primary installation flow.

### CLI / Headless

The `ouroboros` command attaches to the local runtime by default and starts one when `--start` is passed. It exposes managed tasks, progress streams, artifacts, logs, schedules, settings, skills, and evolution controls without duplicating the server's business logic.

```bash
ouroboros status
ouroboros run --start "2+2?"
ouroboros run "Summarize current runtime state"
ouroboros run --workspace /path/to/project --memory-mode forked --patch-out result.patch "Fix the failing test"
ouroboros tasks list
ouroboros logs tail progress --task-id <task_id>
ouroboros schedule add --name nightly-review --cron "0 2 * * *" "Run a maintenance review"
ouroboros schedule list
```

External workspaces must be separate Git worktree roots and may not overlap Ouroboros's own repository or data directory. Patch, streaming, detached-task, and schedule semantics are documented in the CLI help and the canonical [architecture](docs/ARCHITECTURE.md).

### For Agents

Another agent, script, or CI job can invoke Ouroboros through the same gateway-backed CLI:

```bash
ouroboros run --start \
  --workspace /path/to/project \
  --memory-mode forked \
  --patch-out result.patch \
  --result-json-out result.json \
  "Investigate the task, act, and verify the result"
```

Use `--jsonl` for a machine-readable event stream and `--detach` when the caller will follow the task with `ouroboros tasks watch <task_id>` or inspect it with `ouroboros tasks show <task_id>`. External workspace runs keep Ouroboros's own repository and governance context separate, then export changes as reviewable patch artifacts.

To change Ouroboros itself, follow [CONTRIBUTING.md](CONTRIBUTING.md) and read [BIBLE.md](BIBLE.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), and [docs/CHECKLISTS.md](docs/CHECKLISTS.md) in full before editing.

### Configuration

The first-run wizard and **Settings** configure model access, cognitive roles, local models, review policy, runtime mode, budget, skills, and optional integrations. Ouroboros supports configurable remote providers, compatible endpoints, and local GGUF inference; exact settings and defaults live in [`ouroboros/config.py`](ouroboros/config.py) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

The server binds to `127.0.0.1:8765` by default. Read [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) before exposing it beyond loopback; non-local binds need `OUROBOROS_NETWORK_PASSWORD` or an explicitly trusted external access layer.

### Run Tests

```bash
make test
```

---

## Build

### Docker

```bash
docker build -t ouroboros-web .
docker run --rm -p 8765:8765 \
  -e OUROBOROS_NETWORK_PASSWORD='choose-a-password' \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

Docker runs the web runtime, not the native desktop shell. It bundles Chromium and WebKit support; use [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for network and container policy.

### Release tag prerequisite

Platform build scripts package only a commit already tagged with `v$(cat VERSION)`. Tag the exact release commit first:

```bash
git tag -a "v$(tr -d '[:space:]' < VERSION)" -m "Release v$(tr -d '[:space:]' < VERSION)"
```

`scripts/build_repo_bundle.py` verifies the tag and embeds the source binding into the packaged repository bundle. Signing, notarization, bytecode sealing, and CI invariants are documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

### macOS (.dmg)

```bash
bash scripts/download_python_standalone.sh
OUROBOROS_SIGN=0 bash build.sh
```

Output: `dist/Ouroboros-<VERSION>.dmg`, containing `Ouroboros.app` and `Install CLI.command`. Omit `OUROBOROS_SIGN=0` when a Developer ID signing identity is configured.

### Linux (.tar.gz)

```bash
bash scripts/download_python_standalone.sh
bash build_linux.sh
```

Output: `dist/Ouroboros-<VERSION>-linux-<arch>.tar.gz`, containing `Ouroboros/bin/install-ouroboros-cli`. If bundled browser tools need host libraries, run `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit`.

### Windows (.zip)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

Output: `dist\Ouroboros-<VERSION>-windows-x64.zip`, containing `Ouroboros\bin\install-ouroboros-cli.cmd`.


## Architecture and Runtime Data

The native launcher starts a web runtime and supervisor-managed agent workers. The agent core lives in `ouroboros/`, the interface in `web/`, the process plane in `supervisor/`, and the runtime's durable identity, state, history, logs, and skills under `~/Ouroboros/data/`.

The full component map, data flow, API surface, storage layout, safety boundary, and operational rationale live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Deployment details live in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Runtime Commands

| Command | Purpose |
|---------|---------|
| `/panic` | Stop the runtime and its managed processes immediately. |
| `/restart` | Restart without automatically resuming the active owner task. |
| `/status` | Show workers, task queue, and budget state. |
| `/evolve on\|off` | Start or stop autonomous evolution. |
| `/review` | Queue a deep constitutional and architectural self-review. |
| `/bg start\|stop\|status` | Control background consciousness. |


## Philosophy

The 13 Constitution principles — Agency, Continuity, Meta-over-Patch,
Immune Integrity, Self-Creation, LLM-First, Authenticity & Reality
Discipline, Minimalism, Becoming, Versioning and Releases, the absorbed
Iterations / Spiral lineage, and Epistemic Stability — are defined in
full in [`BIBLE.md`](BIBLE.md). That file is the constitutional SSOT
(Bible P4 Ship-of-Theseus protection) and this README intentionally does
not paraphrase it.

---

## Contributing

External contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the complete workflow. Open pull requests against the lowercase
`ouroboros` branch and leave release-version allocation to maintainers. A
current OpenRouter triad + scope packet is the optional fast path; pull
requests without one remain welcome but require more maintainer-side review
and integration work.

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 6.87.5 | 2026-08-01 | **fix: benchmark visuals keep their real geometry and final harness logos on every screen.** Media images now preserve their intrinsic aspect ratio instead of retaining a fixed HTML height when CSS narrows them. Terminal-Bench, OSWorld, and CL-Bench use the final vector artwork rather than low-density raster exports; the accepted transparent Ouroboros, Claude, OpenAI/Codex, and Cursor marks are embedded directly in each SVG, because browsers suppress nested external resources when an SVG is loaded through an image element. Content fingerprints invalidate stale browser caches, the asset sync starts from a clean target, and the committed Pages output is rebuilt. Pixel comparison against the final Habr PNGs is exact, with desktop and mobile visual checks covering logo identity, text, chips, bars, and error whiskers. |
| 6.87.4 | 2026-07-31 | **docs: the README and the public site carry the benchmark evidence.** The README gains a Benchmarks section — the Terminal-Bench 2.1, OSWorld-Verified, and CL-Bench state-of-the-art rows with model-matched comparisons, the SWE-bench Pro and GAIA parity rows, and links to submissions, public traces, and per-benchmark methodology. The homepage gets an evidence chapter with the headline charts and the same links, and both surfaces replace the April interface captures with current ones: the live subagent swarm, a project room with a built-and-verified game, the OuroborosHub skills page, and the code-growth chart. The README website badge moves to ouroboros-agent.ai, and the site metadata and social previews now name the benchmark results. |
| 6.87.3 | 2026-07-31 | **fix: routing tools prove their effect instead of treating a queue write as success.** A manager-backed event bus lives for the server process, survives worker-pool replacement and force-killed producers, and serializes writes before returning. Pool startup is atomic, managed-update recovery never overwrites a live generation, and crash-storm disablement is a durable admission fence. Promote, project-route, manual-target, and steer actions carry a unique token and wait for the supervisor's durable receipt; only a receipt for that exact attempt permits a positive final. Promoted/API tasks additionally require a persisted queue snapshot and scheduled task result, reject duplicate ids, and resolve source clone/attach only after authoritative executor admission. Rejections and 15-second unconfirmed outcomes are loud, self-contained, and never invite automatic retry. Real child-process and end-to-end transport regressions cover every routing outcome plus concurrent pool startup and API snapshot failure. |
| 6.87.2 | 2026-07-31 | **fix: Telegram Mini App recovers a completed menu rollback and keeps the real Quick Tunnel failure visible during backoff.** An interrupted or older rollback could leave its ownership snapshot behind after Telegram had already restored the original button, so every later URL rotation was rejected as external drift. The exact original is now recognized as a completed rollback while any third value remains fail-closed. Cloudflared's bounded, redacted final error line survives reconnect backoff instead of being replaced by a generic status. The bundled Telegram skill moves to 1.0.1 so existing native installs resync the fix. |
| 6.87.1 | 2026-07-31 | **fix: a clean review verdict stops being recorded as unparseable, and the release lane goes green again.** The shared prompt contract asks a reviewer that found nothing for an empty array plus the `NO_FINDINGS` sentinel; triad implemented it and the advisory parser never had a branch for it, so a reviewer returning exactly what was asked was recorded as `parse_failure` — blocking freshness, paying an extra extraction model, and forcing a retry that re-ran the full serial preflight. The contract text now lives beside the parser that enforces it and splits into findings-only and required-matrix modes, so scope review and skill advisory stop advertising an all-clear their own parsers reject, and `review_status` surfaces the typed per-run cause it already persisted. Three CI-only test defects that predate this change were fixed with it: a 1s subprocess budget that could not cover interpreter startup on Windows, a POSIX permission assertion evaluated on Windows, and a fixed 220ms sleep racing a 180ms drawer transition on the Linux WebKit runner. |
| 6.87.0 | 2026-07-31 | **feat: the OSWorld working prompt separates a task's live surface from its stored one, and stops the contract's UNCHANGED item from forbidding the very edit the task asks for.** Two failures on the v6.86.0 run traced to the same paragraph. A task whose grader reads the LIVE window (VLC fullscreen) was answered by ticking the preference and never entering fullscreen, because the contract asked only where the result must PERSIST; WHERE now has two slots, live and persisted, each filled or explicitly marked not-applicable — and filling a slot must never invent an extra tab, window or dialog to inspect, which would break the 28 tasks graded on tab lists alone. A task asking for a bullet on an existing paragraph was answered by typing a new line, because the contract had recorded the existing paragraph as UNCHANGED; UNCHANGED now covers only content the task does not mention, and a MARKER or PROPERTY the task names — a bullet, a style, a colour, an alignment — is applied to the content already there rather than to a freshly typed line. Three further candidate clauses were written and dropped after adversarial review proved each would cost more than it won: preferring a slide master over instances loses a task whose shapes carry direct formatting that overrides styles, demanding a named resource be exhausted before any substitute loses a task won by detouring to another search engine, and requiring a configuration CLI to match the GUI's breadth was generalised from a single grader and contradicted the preamble's own rule against shaping work around guesses at the evaluator. |
| 6.86.0 | 2026-07-30 | **feat: the OSWorld working prompt gains an atomic task contract, and a task's proxy session stops leaking into the published tree.** Forensics on the v6.84.0 run (89.05%) against the leaderboard leader's own published per-task dump showed the gap is 19 tasks, 8 of them one class: the work was done and never checked against the surface the grader reads. The agent now writes the task's obligations as a numbered checklist BEFORE its first mutating action — object, required state with every stated qualifier, order, what must stay unchanged, where the result must persist — and closes each item as observed-satisfied / not-verified / impossible before it may finish, with at most one targeted repair. Plural instructions still cover every matching element; only a SINGULAR referent resolving to several candidates forces a justified choice of one, and the contract is explicitly revisable when observation contradicts it. Three infeasibility shapes are named: discovery that falls outside a stated means restriction, a named mode of operation the application does not ship, and a mechanism whose trigger is narrower than the task states. The desktop environment's own configuration CLI (gsettings/dconf) is declared a legitimate surface — it writes the same store the Settings app does — while private application state stays forbidden. The colour clause drops a motivation that was simply untrue on a graded task (the metric there measures distance from a pure primary and reads no reference file at all). Harness: each proxy-flagged task now gets its own sticky upstream session keyed on campaign root plus example id, so two concurrent campaigns never share an exit; that config carries the account password and is therefore written to LANE-PRIVATE state and unlinked afterwards, never under `results/`, which is the tree that gets archived and published — an earlier draft of this same change wrote it into all 361 result directories. After the post-gate reset the runner also probes whether the binaries a task's setup claims to install are actually present and records the answer in the manifest, because upstream reports a guest command that failed as "executed successfully", and a premise that vanished between gate and worker otherwise surfaces as an honest infeasible scored zero. |
| 6.85.0 | 2026-07-30 | **feat: Telegram becomes a first-party native capability, and blocked skill repairs regain a valid completion path.** (1) The bundled `telegram` skill consolidates the proven owner-only text/photo bridge and Mini App PoC without migrating, disabling, deleting, or changing either legacy payload. It preserves the existing bridge commands, outbound media, cards, opt-in notifications, mirror-all behavior, Ouroboros SPA, private first-contact binding, process-memory sessions, pinned Quick Tunnel lifecycle, menu rollback, and platform limits; the Mini App may be disabled or unavailable while the text bridge remains loaded. Native trust is hash-bound, while the bot token and privileged host permissions still require the normal Grant access then enable flow. (2) Bounded manifest `conflicts` declarations are enforced symmetrically at enable, reconcile, startup, and dispatch, returning a typed conflict without automatic state transfer. The Skills card now says `Loaded` for extension registration instead of overstating readiness as `Active`, and Telegram reports bridge and Mini App status in its own surface. (3) Typed `skill_repair` requests are promoted to managed tasks before ephemeral routing, preserving payload confinement, review access, and `allow_enable=false`; ordinary ephemeral default-deny policy is unchanged. (4) Google Colab discovers the seeded native Telegram skill, waits for a fresh executable native verdict, grants only API-reported missing grantable items under the persisted owner policy, enables it, and saves the proven full-access, mirror, and Mini App defaults without a Hub install or extra review. |
| 6.84.0 | 2026-07-30 | **fix: the OSWorld working prompt stops charging the wrong resource, and three of its own clauses stop costing points.** Forensics over every failed task of the v6.83.0 dual run (74 agents, whole-loss coverage: the per-task deltas sum to the measured 12.90 pp) found the most expensive defect was ours: the preamble said "every tool call costs ~30s, so MINIMIZE calls" while the budget is denominated in TURNS and the official contract batches actions inside one `predict()`. The agent obeyed — 1.01 tool calls per turn across 11k turns, i.e. the benchmark ran on about a third of its action budget. The clause is now turn-denominated and asks for 4-8 confident calls in one turn, split only where a target depends on what the previous action reveals. Three clauses the agent CITED while losing are corrected: an exact value now beats the app's named swatch (a palette "Blue" 2A6099 is not 0000FF); "already in the requested state" must be judged from the STORED value, since controls render defaults as selected while nothing is stored; and ordinals no longer blanket-exclude headings, which on a slide are often the counted item. The command line is restored as the right tool for genuine batch/file work (one `pdfseparate` instead of N print dialogs) while GUI stays mandatory for application state. Added: verification by independent read-back (re-open the saved artifact, read it with a different tool than wrote it) merged with the minimal-diff rule into one clause; and a premise branch — a task asking for something VISIBLE is not satisfied by storing a flag, and an agent writing that the real path is impossible or that it is delivering a stand-in has already found its verdict. Adapter/prompt only. Harness: `evaluate()` now runs with the checkout root as CWD, because evaluator fixtures are declared relative and `get_local_file` tests them against the process CWD — one task produced a byte-exact answer and scored 0; the gate's UNUSED turn reserve is returned to the worker (the gate budgets 14 and spends ~4, and 13 of 56 failures died at 89-92 turns inside a 100-turn budget); and proxy support is gated on a LIVE CONNECT probe rather than the config file existing, with a proxy:true task whose trace shows an exhausted upstream quarantined as infrastructure instead of scored as a capability zero. |
| 6.83.0 | 2026-07-30 | **fix: a screenshot that cannot be decoded fails where it is taken, an infeasibility verdict is judged as an argument, and a declared step budget is one the runtime actually enforces.** (1) Image integrity is fail-closed at three seams: the remote screenshot fetch validates a FULL decode before publishing a path (bounded re-fetch, write-validate-rename), the shared remote-result builder rejects an undecodable capture instead of claiming ok, and the VLM payload builder raises `IMAGE_UNDECODABLE` at build time. A truncated PNG keeps a valid 24-byte header, so header-only checks passed it and it detonated rounds later as a non-retryable provider 400 — five task deaths in the v6.81.1 OSWorld run. Four test fixtures labelled "minimal valid PNG" were themselves undecodable and are now real images; one assertion that pinned an IDENTITY coordinate transform for a 1920x1080 capture at a 1280 cap (it only held because the stub never downscaled) now pins the real 1.5x transform. (2) Tool results are judged by their typed envelope: a structured `{"ok": false}` payload counts as a failure for the error counters, anti-loop and auto-attach, instead of only text markers. (3) Acceptance review gains an ABSENT-PREMISE branch: when the terminal claim is that the premise is missing, the deliverable under review is the PREMISE ARGUMENT — instantiating "the named artifact exists" as a criterion begs the question, and coaching a continuation whose remaining routes breach the task's own stated restrictions manufactures the artifact the task forbids. A weak premise argument still fails on its own grounds. Measured cost of the old behaviour: a correct 1.0 converted into 0.0 over 149 tool calls. (4) `type_text` routes multi-line and long payloads through the in-VM clipboard (typewrite presses Enter per newline and sheds keystrokes), joining the non-ASCII and angle-bracket paths. (5) OSWorld adapter: `--max-steps` declares AND enforces a leaderboard-comparable budget — a step is one top-level policy turn, matching the official `predict() -> actions[]` boundary, not one GUI action; the server round cap is verified against the derived worker cap before the VM boots, the gate phase is cancelled at its own reserve (the runtime cap is server-wide and the gate is a separate task), and the post-run audit reads the loop's policy turns rather than the flat physical-call field, which disagreed with it on 344 of 346 examples. `--expect-dataset-commit` turns the graded-spec pin into a gate: a checkout other than the campaign's is refused before any paid work, because it supplies different task instructions AND a different evaluator. |
Older releases are preserved in Git tags and GitHub releases. Older 6.x rows (including 6.86.1, 6.81.1, 6.76.0, 6.75.0, 6.74.5, 6.74.4, 6.74.1, 6.74.0, 6.73.2, 6.73.1, 6.73.0, 6.72.0, 6.71.2, 6.71.1, 6.71.0, 6.70.0, 6.69.0, 6.68.0, 6.67.0, 6.66.0, 6.65.4, 6.65.3, 6.65.2, 6.65.1, 6.65.0, 6.64.3, 6.64.2, 6.64.1, 6.64.0, 6.63.0, 6.62.0, 6.61.4, 6.61.3, 6.61.1, 6.61.0, 6.60.0, 6.59.0, 6.58.0, 6.57.0, 6.56.0, 6.55.0, 6.54.4, 6.54.2, 6.54.1, 6.54.0, 6.53.4, 6.53.0, 6.51.0), the 5.2.0 through 5.33.0-rc.6 rows, and former `4.0.0` rows are rolled off to respect the P9 changelog cap; their full bodies remain at their git tags.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev
