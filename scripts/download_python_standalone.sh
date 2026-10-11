#!/bin/bash
set -euo pipefail

# Downloads python-build-standalone for macOS (arm64 + x86_64) and Linux (x86_64)
# Run from repo root: bash scripts/download_python_standalone.sh

RELEASE="20260211"
PY_VERSION="3.10.19"
DEST="python-standalone"

OS=$(uname -s)
ARCH=$(uname -m)

if [ "$OS" = "Darwin" ]; then
    if [ "$ARCH" = "arm64" ]; then
        PLATFORM="aarch64-apple-darwin"
        SHA256="e6634b06afa2ae79e664cf34174dad5d31b30117f90a01d533de3f4e9db6974e"
    elif [ "$ARCH" = "x86_64" ]; then
        PLATFORM="x86_64-apple-darwin"
        SHA256="8d08ff2d9bb20566223f20307c8d9c31bdf020b1f1a237c2aa6efd1de651bf7b"
    else
        echo "Unsupported macOS architecture: $ARCH"
        exit 1
    fi
elif [ "$OS" = "Linux" ]; then
    if [ "$ARCH" = "x86_64" ]; then
        PLATFORM="x86_64-unknown-linux-gnu"
        SHA256="d71df61d1cdb59af4443912da8eeca744a52e782ff5deefa966ead893235a39e"
    elif [ "$ARCH" = "aarch64" ]; then
        PLATFORM="aarch64-unknown-linux-gnu"
        SHA256="f2916a20f3de5500df5129e37dd0a213281dea29e226ec1ffe7fecdb10955533"
    else
        echo "Unsupported Linux architecture: $ARCH"
        exit 1
    fi
else
    echo "Unsupported OS: $OS"
    exit 1
fi

FILENAME="cpython-${PY_VERSION}+${RELEASE}-${PLATFORM}-install_only_stripped.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/${FILENAME}"

echo "=== Downloading Python ${PY_VERSION} for ${PLATFORM} ==="
echo "URL: ${URL}"

rm -rf "$DEST" _python_tmp
mkdir -p _python_tmp

# Pinned SHA256 (from the release's SHA256SUMS): a swapped/truncated archive
# fails here instead of becoming the packaged runtime. Update the pins when
# bumping RELEASE/PY_VERSION.
curl -L --fail --progress-bar "$URL" -o _python_tmp/"$FILENAME"
if command -v shasum >/dev/null 2>&1; then
    ACTUAL="$(shasum -a 256 "_python_tmp/${FILENAME}" | awk '{print $1}')"
else
    ACTUAL="$(sha256sum "_python_tmp/${FILENAME}" | awk '{print $1}')"
fi
if [ "$ACTUAL" != "$SHA256" ]; then
    echo "SHA256 mismatch for ${FILENAME}: expected ${SHA256}, got ${ACTUAL} — refusing to install."
    exit 1
fi
tar xz -C _python_tmp -f _python_tmp/"$FILENAME"
rm -f _python_tmp/"$FILENAME"

# Archive extracts to python/ — rename to python-standalone/
mv _python_tmp/python "$DEST"
rm -rf _python_tmp

echo ""
echo "=== Installing agent dependencies ==="
if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv is required for locked dependency installation."
    exit 1
fi
uv pip install --python "${DEST}/bin/python3" --quiet -r requirements-runtime.lock

echo ""
echo "=== Done ==="
echo "Python: ${DEST}/bin/python3"
"${DEST}/bin/python3" --version
