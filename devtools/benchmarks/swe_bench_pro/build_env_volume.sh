#!/usr/bin/env bash
# Build the self-contained `oboros-env` Docker volume that e1v2/run_pro.py mounts
# read-only into each SWE-bench Pro task image at /opt/miniconda3/envs/oboros.
#
# WHY THIS EXISTS
# ---------------
# e1v2/entrypoint_pro.sh runs Ouroboros with OBO_PY=/opt/miniconda3/envs/oboros/bin/python
# and PYTHONPATH=/obo-repo. So the agent's SOURCE comes from the seeded /obo-repo volume,
# while the PYTHON INTERPRETER + third-party DEPENDENCIES come from this `oboros-env`
# volume. The volume was historically hand-built by an external kit; this script makes the
# devtools self-sufficient: it builds the env from THIS repo's runtime lock, pinned to
# whatever checkout the script is run from.
#
# The env contains DEPENDENCIES ONLY (no `pip install -e .`): Ouroboros itself is imported
# from /obo-repo via PYTHONPATH, exactly like the runtime path.
#
# PORTABILITY
# -----------
# A conda env created directly at the prefix /opt/miniconda3/envs/oboros is self-contained
# (its own python + libs live under the prefix), so it mounts read-only into arbitrary
# glibc-based jefzda/sweap-images task containers. It is built with --platform linux/amd64
# to match those images (the env's compiled wheels must match the task image architecture).
#
# USAGE
#   devtools/benchmarks/swe_bench_pro/build_env_volume.sh [--rebuild] [--volume NAME]
#       [--python 3.11] [--platform linux/amd64] [--base-image condaforge/miniforge3:latest]
#       [--src /path/to/ouroboros/repo]
#
# Idempotent: a no-op if the volume already has a working python, unless --rebuild is given.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_SRC="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"   # repo root (devtools/benchmarks/swe_bench_pro -> repo)

VOLUME="oboros-env"
PYVER="3.11"
PLATFORM="linux/amd64"
BASE_IMAGE="condaforge/miniforge3:latest"
SRC="$DEFAULT_SRC"
PREFIX="/opt/miniconda3/envs/oboros"
REBUILD=0

while [ $# -gt 0 ]; do
  case "$1" in
    --rebuild) REBUILD=1; shift ;;
    --volume) VOLUME="$2"; shift 2 ;;
    --python) PYVER="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --base-image) BASE_IMAGE="$2"; shift 2 ;;
    --src) SRC="$2"; shift 2 ;;
    -h|--help) sed -n '1,40p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REQ="$SRC/requirements-runtime.lock"
[ -f "$REQ" ] || { echo "error: requirements-runtime.lock not found at $REQ (use --src)" >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "error: docker not found" >&2; exit 2; }

env_python_ok() {
  docker run --rm -v "$VOLUME:$PREFIX:ro" --platform "$PLATFORM" --entrypoint "$PREFIX/bin/python" \
    "$BASE_IMAGE" -c "import sys; print(sys.version)" >/dev/null 2>&1
}

if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  if [ "$REBUILD" = 1 ]; then
    echo "[build-env] --rebuild: removing existing volume '$VOLUME'"
    docker volume rm -f "$VOLUME" >/dev/null
  elif env_python_ok; then
    echo "[build-env] volume '$VOLUME' already has a working python at $PREFIX; nothing to do (use --rebuild to force)"
    exit 0
  else
    echo "[build-env] volume '$VOLUME' exists but has no working python; rebuilding into it"
    docker volume rm -f "$VOLUME" >/dev/null
  fi
fi
docker volume create "$VOLUME" >/dev/null

echo "[build-env] building $VOLUME from $SRC (python $PYVER, platform $PLATFORM, base $BASE_IMAGE)"
# Mount the volume AT the env prefix and create the conda env directly into it, so the volume
# content is exactly the env (run_pro mounts `oboros-env:/opt/miniconda3/envs/oboros:ro`).
docker run --rm --platform "$PLATFORM" \
  -v "$VOLUME:$PREFIX" \
  -v "$SRC:/src:ro" \
  --entrypoint bash "$BASE_IMAGE" -euo pipefail -c '
    PREFIX="'"$PREFIX"'"; PYVER="'"$PYVER"'"
    echo "[build-env] conda create -p $PREFIX python=$PYVER"
    conda create -y -p "$PREFIX" "python=$PYVER"
    PIP="$PREFIX/bin/pip"
    "$PIP" install --no-input --upgrade pip setuptools wheel
    echo "[build-env] installing requirements-runtime.lock"
    if ! "$PIP" install --no-input -r /src/requirements-runtime.lock; then
      echo "[build-env] full requirements install failed; retrying without tree-sitter code-intel deps (lazy runtime import, degrades gracefully)"
      grep -ivE "tree[-_]sitter" /src/requirements-runtime.lock > /tmp/reqs_no_treesitter.txt
      "$PIP" install --no-input -r /tmp/reqs_no_treesitter.txt
    fi
    # Trim caches that bloat the read-only volume.
    conda clean -y -a >/dev/null 2>&1 || true
    find "$PREFIX" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
    "$PREFIX/bin/python" --version
  '

echo "[build-env] verifying core runtime deps import in the env"
docker run --rm -v "$VOLUME:$PREFIX:ro" --platform "$PLATFORM" --entrypoint "$PREFIX/bin/python" \
  "$BASE_IMAGE" -c '
import importlib.util, sys
mods = ["httpx","starlette","uvicorn","websockets","openai","yaml","dulwich","PIL","huggingface_hub"]
optional = ["tree_sitter","tree_sitter_language_pack","mcp","playwright"]
missing=[m for m in mods if importlib.util.find_spec(m) is None]
opt_missing=[m for m in optional if importlib.util.find_spec(m) is None]
print("python", sys.version.split()[0])
print("required ok:", [m for m in mods if m not in missing])
if missing: print("MISSING REQUIRED:", missing); sys.exit(1)
if opt_missing: print("optional missing (ok, degrades):", opt_missing)
print("[build-env] OK")
'
echo "[build-env] done: volume '$VOLUME' is ready for e1v2/run_pro.py"
