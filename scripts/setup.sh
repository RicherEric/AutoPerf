#!/usr/bin/env bash
# POSIX entry point for the canonical cross-platform installer.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
exec python3 scripts/setup.py "$@"
