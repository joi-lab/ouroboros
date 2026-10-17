# Ouroboros — Docker images for the web UI runtime
# Usage (recipes: packaging/docker/compose.yaml, explained in docs/DEPLOYMENT.md):
#   docker build -t ouroboros-web .
#   docker run --rm -p 8765:8765 ouroboros-web
#   docker build --target managed --build-context ouroboros-seed=<seed dir> -t ouroboros-managed .
# The RUN --mount caches and --build-context need BuildKit, Docker's default builder.

FROM ghcr.io/astral-sh/uv:0.12.1 AS uv
FROM python:3.10-slim AS runtime

COPY --from=uv /uv /uvx /bin/

# Browsers first, dependencies second, sources last: every release rewrites
# pyproject.toml/uv.lock, so anything below the lock copy is rebuilt per
# release while the apt packages and the Chromium/WebKit downloads above it
# are reused. The Playwright pin must equal the locked version so the
# downloaded browser revisions match the venv's driver
# (tests/test_build_scripts.py::TestDockerfile). The installer runs from an
# ephemeral uvx tool environment, so the image carries one Playwright: the
# venv's. Browsers live in a shared path the runtime honors as-is
# (ouroboros/tools/browser.py) — not inside the package tree, which the
# per-release dependency layer would rebuild.
ARG PLAYWRIGHT_VERSION=1.62.0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# System dependencies: git for the agent's own history and updates, plus
# every Chromium/WebKit native library from Playwright's authoritative list.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends git \
    && uvx --from "playwright==${PLAYWRIGHT_VERSION}" playwright install-deps chromium webkit \
    && uvx --from "playwright==${PLAYWRIGHT_VERSION}" playwright install chromium webkit

# Working directory
ENV APP_HOME=/app \
    PATH="/app/.venv/bin:$PATH"
WORKDIR ${APP_HOME}

# Resolve only from the reviewed lock; the project itself is installed after
# the source copy so source edits reuse this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra browser --no-install-project

# Copy application
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra browser --no-editable

# Default environment
ENV OUROBOROS_SERVER_HOST=0.0.0.0 \
    OUROBOROS_SERVER_PORT=8765 \
    OUROBOROS_FILE_BROWSER_DEFAULT=${APP_HOME}

EXPOSE 8765

ENTRYPOINT ["python", "server.py"]

# Optional persistent managed body: this image only hosts the common launcher. A
# released seed (repo.bundle + repo_bundle_manifest.json from
# scripts/build_repo_bundle.py) bootstraps the body checkout and its runtime venv
# on volumes, so Update, self-modification and dependencies survive recreation.
FROM runtime AS managed
ENV OUROBOROS_APP_ROOT=/ouroboros \
    OUROBOROS_REPO_DIR=/ouroboros/body/repo \
    OUROBOROS_DATA_DIR=/ouroboros/data \
    OUROBOROS_PID_FILE=/ouroboros/data/ouroboros.pid \
    OUROBOROS_SUBAGENT_PROJECTS_ROOT=/ouroboros/work/projects \
    OUROBOROS_SUBAGENT_WORKTREE_ROOT=/ouroboros/work/subagent_worktrees \
    OUROBOROS_DELIVERABLES_ROOT=/ouroboros/work/Deliverables \
    OUROBOROS_FILE_BROWSER_DEFAULT=/ouroboros/work \
    HOME=/ouroboros/home
COPY --from=ouroboros-seed repo.bundle repo_bundle_manifest.json /opt/ouroboros/seed/
# The seed must carry complete history (first boot clones it; Update and rollback
# walk it); its VERSION is the manifest's. Integrity is re-proved at every boot.
RUN python -c "import json, pathlib; seed = pathlib.Path('/opt/ouroboros/seed'); (seed / 'VERSION').write_text(json.loads((seed / 'repo_bundle_manifest.json').read_text())['app_version'] + '\\n')" \
    && git init -q /tmp/seed-check && git -C /tmp/seed-check bundle verify /opt/ouroboros/seed/repo.bundle \
    && rm -rf /tmp/seed-check
ENTRYPOINT ["python", "/app/launcher.py", "--no-ui", "--seed-bundle", "/opt/ouroboros/seed", "--runtime-venv", "/ouroboros/body/venv"]
CMD ["--launch-intent", "automatic"]

# The ordinary immutable image stays the default target.
FROM runtime
