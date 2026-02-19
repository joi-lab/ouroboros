#!/usr/bin/env bash
# =============================================================================
# Ouroboros — One-shot setup script for Ubuntu 22.04+ (Azure VM)
# =============================================================================
# Usage:
#   sudo bash deploy/setup.sh
#
# What this script does:
#   1. Installs system dependencies (Python 3.11+, Node.js, git, gh CLI, etc.)
#   2. Creates the 'ouroboros' system user
#   3. Clones the repository to /opt/ouroboros/repo
#   4. Creates a Python venv and installs pip requirements
#   5. Installs Playwright and its Chromium browser
#   6. Installs Claude Code CLI (optional, requires ANTHROPIC_API_KEY)
#   7. Creates the data directory structure
#   8. Copies .env.example to /opt/ouroboros/.env for editing
#   9. Installs and enables the systemd service
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment before running this script
# ---------------------------------------------------------------------------
OUROBOROS_USER="${OUROBOROS_USER:-ouroboros}"
INSTALL_DIR="${INSTALL_DIR:-/opt/ouroboros}"
REPO_URL="${REPO_URL:-https://github.com/razzant/ouroboros.git}"
REPO_BRANCH="${REPO_BRANCH:-ouroboros}"
PYTHON_VERSION="${PYTHON_VERSION:-python3.11}"

REPO_DIR="${INSTALL_DIR}/repo"
VENV_DIR="${INSTALL_DIR}/venv"
DATA_DIR="${INSTALL_DIR}/data"
ENV_FILE="${INSTALL_DIR}/.env"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo -e "\033[1;34m[setup]\033[0m $*"; }
warn()  { echo -e "\033[1;33m[warn]\033[0m $*"; }
error() { echo -e "\033[1;31m[error]\033[0m $*"; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)."
    fi
}

# ---------------------------------------------------------------------------
# 1) System dependencies
# ---------------------------------------------------------------------------
install_system_deps() {
    info "Installing system dependencies..."
    apt-get update -qq

    # Core tools
    apt-get install -y -qq \
        software-properties-common \
        curl wget git jq unzip \
        build-essential

    # Python 3.11+
    if ! command -v "${PYTHON_VERSION}" &>/dev/null; then
        add-apt-repository -y ppa:deadsnakes/ppa
        apt-get update -qq
    fi
    apt-get install -y -qq \
        "${PYTHON_VERSION}" \
        "${PYTHON_VERSION}-venv" \
        "${PYTHON_VERSION}-dev" \
        python3-pip

    # Node.js 20.x (for Claude Code CLI)
    if ! command -v node &>/dev/null; then
        info "Installing Node.js 20.x..."
        curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
        apt-get install -y -qq nodejs
    fi

    # GitHub CLI
    if ! command -v gh &>/dev/null; then
        info "Installing GitHub CLI..."
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
            | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
            | tee /etc/apt/sources.list.d/github-cli.list >/dev/null
        apt-get update -qq
        apt-get install -y -qq gh
    fi

    # Playwright system dependencies (for headless Chromium)
    info "Installing Playwright OS dependencies..."
    apt-get install -y -qq \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libatspi2.0-0 libwayland-client0 \
        xvfb || warn "Some Playwright deps may be missing; playwright install --with-deps will fix this."
}

# ---------------------------------------------------------------------------
# 2) Create system user
# ---------------------------------------------------------------------------
create_user() {
    if id "${OUROBOROS_USER}" &>/dev/null; then
        info "User '${OUROBOROS_USER}' already exists."
    else
        info "Creating system user '${OUROBOROS_USER}'..."
        useradd --system --create-home --home-dir "${INSTALL_DIR}" \
                --shell /bin/bash "${OUROBOROS_USER}"
    fi
}

# ---------------------------------------------------------------------------
# 3) Clone repository
# ---------------------------------------------------------------------------
clone_repo() {
    if [[ -d "${REPO_DIR}/.git" ]]; then
        info "Repository already exists at ${REPO_DIR}, pulling latest..."
        sudo -u "${OUROBOROS_USER}" git -C "${REPO_DIR}" fetch origin
        sudo -u "${OUROBOROS_USER}" git -C "${REPO_DIR}" checkout "${REPO_BRANCH}"
        sudo -u "${OUROBOROS_USER}" git -C "${REPO_DIR}" reset --hard "origin/${REPO_BRANCH}"
    else
        info "Cloning repository to ${REPO_DIR}..."
        mkdir -p "${REPO_DIR}"
        chown "${OUROBOROS_USER}:${OUROBOROS_USER}" "${REPO_DIR}"
        sudo -u "${OUROBOROS_USER}" git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${REPO_DIR}"
    fi
}

# ---------------------------------------------------------------------------
# 4) Python venv + requirements
# ---------------------------------------------------------------------------
setup_venv() {
    info "Setting up Python virtual environment..."
    if [[ ! -d "${VENV_DIR}" ]]; then
        sudo -u "${OUROBOROS_USER}" "${PYTHON_VERSION}" -m venv "${VENV_DIR}"
    fi

    info "Installing Python requirements..."
    sudo -u "${OUROBOROS_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel -q
    sudo -u "${OUROBOROS_USER}" "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements.txt" -q
}

# ---------------------------------------------------------------------------
# 5) Playwright Chromium
# ---------------------------------------------------------------------------
install_playwright() {
    info "Installing Playwright Chromium..."
    sudo -u "${OUROBOROS_USER}" "${VENV_DIR}/bin/playwright" install chromium --with-deps || \
        warn "Playwright chromium install failed; browser tools will be unavailable."
}

# ---------------------------------------------------------------------------
# 6) Claude Code CLI (best-effort)
# ---------------------------------------------------------------------------
install_claude_code() {
    info "Installing Claude Code CLI (best-effort)..."
    if command -v npm &>/dev/null; then
        npm install -g @anthropic-ai/claude-code 2>/dev/null || \
            warn "Claude Code CLI install failed; will retry at runtime if ANTHROPIC_API_KEY is set."
    else
        warn "npm not found; skipping Claude Code CLI install."
    fi
}

# ---------------------------------------------------------------------------
# 7) Data directory structure
# ---------------------------------------------------------------------------
create_data_dirs() {
    info "Creating data directory structure at ${DATA_DIR}..."
    for sub in state logs memory memory/knowledge index locks archive; do
        mkdir -p "${DATA_DIR}/${sub}"
    done
    chown -R "${OUROBOROS_USER}:${OUROBOROS_USER}" "${DATA_DIR}"
}

# ---------------------------------------------------------------------------
# 8) Environment file
# ---------------------------------------------------------------------------
setup_env_file() {
    if [[ -f "${ENV_FILE}" ]]; then
        info ".env file already exists at ${ENV_FILE}, skipping."
        info "To reconfigure, edit ${ENV_FILE} directly."
    else
        info "Copying .env.example to ${ENV_FILE}..."
        cp "${REPO_DIR}/.env.example" "${ENV_FILE}"
        chown "${OUROBOROS_USER}:${OUROBOROS_USER}" "${ENV_FILE}"
        chmod 600 "${ENV_FILE}"
        echo ""
        warn "ACTION REQUIRED: Edit ${ENV_FILE} and fill in your API keys."
        warn "  sudo nano ${ENV_FILE}"
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# 9) systemd service
# ---------------------------------------------------------------------------
install_service() {
    info "Installing systemd service..."
    cp "${REPO_DIR}/deploy/ouroboros.service" /etc/systemd/system/ouroboros.service
    systemctl daemon-reload
    systemctl enable ouroboros.service

    info "Service installed. To start:"
    info "  sudo systemctl start ouroboros"
    info "  sudo journalctl -u ouroboros -f"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    require_root

    info "=== Ouroboros Setup for Azure VM ==="
    info "Install dir: ${INSTALL_DIR}"
    info "Repository:  ${REPO_URL} (branch: ${REPO_BRANCH})"
    info "Python:      ${PYTHON_VERSION}"
    echo ""

    install_system_deps
    create_user
    clone_repo
    setup_venv
    install_playwright
    install_claude_code
    create_data_dirs
    setup_env_file
    install_service

    echo ""
    info "=== Setup complete ==="
    info ""
    info "Next steps:"
    info "  1. Edit ${ENV_FILE} with your API keys"
    info "  2. Start the service: sudo systemctl start ouroboros"
    info "  3. Check logs: sudo journalctl -u ouroboros -f"
    info ""
}

main "$@"
