#!/usr/bin/env bash
#
# Sets up a local AutoPerf dev environment on macOS: creates ./venv, does an
# editable install of all optional extras, and installs the frontend's npm
# deps. See docs/INSTALL.md for the full manual walkthrough this mirrors.
#
# By default this only *checks* for adb/Node.js/Redis and prints install
# instructions -- it does not touch anything outside the repo. Pass
# --install-deps to also install those three via Homebrew.

set -euo pipefail

INSTALL_DEPS=0
for arg in "$@"; do
    case "$arg" in
        --install-deps) INSTALL_DEPS=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
warn() { printf '    \033[33m%s\033[0m\n' "$1"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- Python venv -------------------------------------------------------
step "Python virtual environment (./venv)"
if [ ! -x "$REPO_ROOT/venv/bin/python" ]; then
    if ! have python3; then
        warn "python3 not found. Install it first:"
        warn "  brew install python@3.12"
        exit 1
    fi
    version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    major="${version%%.*}"; minor="${version##*.}"
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; }; then
        warn "Python 3.11+ required, found $version. Install it with:"
        warn "  brew install python@3.12"
        exit 1
    fi
    python3 -m venv "$REPO_ROOT/venv"
    ok "Created venv (Python $version)"
else
    ok "venv already exists"
fi
VENV_PY="$REPO_ROOT/venv/bin/python"

# --- Editable install ----------------------------------------------------
step "Installing AutoPerf (editable, all extras)"
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -e ".[dashboard,worker,livescreen,dev]"
ok "Python package installed"

# --- adb -----------------------------------------------------------------
step "Android platform-tools (adb)"
if have adb; then
    ok "adb found: $(command -v adb)"
elif [ "$INSTALL_DEPS" -eq 1 ] && have brew; then
    brew install android-platform-tools
else
    warn "adb not found on PATH. Install it with:"
    warn "  brew install android-platform-tools"
    warn "(or re-run this script with --install-deps)"
fi

# --- Node.js (dashboard only) ---------------------------------------------
step "Node.js (dashboard frontend)"
if have node; then
    ok "node found: $(node --version)"
    step "Installing frontend npm dependencies"
    (cd "$REPO_ROOT/webapp/frontend" && npm install)
elif [ "$INSTALL_DEPS" -eq 1 ] && have brew; then
    brew install node
    warn "Re-run this script once Node.js is on PATH to install npm deps."
else
    warn "node not found on PATH -- skipping frontend npm install."
    warn "  brew install node"
    warn "(or re-run this script with --install-deps)"
fi

# --- Redis (dashboard only) ------------------------------------------------
step "Redis (Celery broker, dashboard only)"
if (exec 3<>/dev/tcp/localhost/6379) 2>/dev/null; then
    exec 3<&- 3>&-
    ok "Something is already listening on localhost:6379"
elif [ "$INSTALL_DEPS" -eq 1 ] && have brew; then
    brew install redis
    brew services start redis
    ok "Installed and started Redis via Homebrew"
else
    warn "No Redis detected on localhost:6379. Start one with:"
    warn "  brew install redis && brew services start redis"
    warn "  (or) docker run -d --name autoperf-redis -p 6379:6379 redis:7-alpine"
fi

step "Done"
echo "Next steps:"
echo "  adb devices"
echo "  ./venv/bin/autoperf devices"
echo "See docs/INSTALL.md for how to start the dashboard, Celery worker, and frontend."
