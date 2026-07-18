#!/usr/bin/env python3
"""Cross-platform AutoPerf setup: detects Windows / macOS / native Linux /
WSL and does the same three things regardless of environment:

  1. create ./venv with a Python >= 3.11 it finds on the system
  2. editable-install autoperf with all optional extras into that venv
  3. `npm install` the dashboard frontend, if Node.js is available

By default it only *checks* for adb/Node.js/Redis and prints the right
install command for the detected platform. Pass --install-deps to also
run those install commands (winget on Windows, Homebrew on macOS,
apt on Ubuntu/Debian/WSL) automatically.

This script only needs whatever Python interpreter you run it with -- it
does not require a venv or third-party packages itself. See
docs/INSTALL.md for the manual walkthrough this mirrors.

Usage:
    python3 scripts/setup.py [--install-deps]
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 11)


def step(msg):
    print("\n==> " + msg)


def ok(msg):
    print("    " + msg)


def warn(msg):
    print("    ! " + msg)


def run(cmd, **kwargs):
    print("    $ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kwargs)


def have(name):
    return shutil.which(name) is not None


def detect_os():
    """Returns one of: 'windows', 'macos', 'wsl', 'linux'."""
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        release = platform.uname().release.lower()
        if "microsoft" in release or os.environ.get("WSL_DISTRO_NAME"):
            return "wsl"
        return "linux"
    raise RuntimeError("Unsupported platform: " + system)


def python_version(cmd_prefix):
    try:
        out = subprocess.run(
            cmd_prefix + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        major, minor = (int(part) for part in out.stdout.strip().split("."))
    except ValueError:
        return None
    return (major, minor)


def find_python(target_os):
    """Finds the newest Python >= MIN_PYTHON on PATH, preferring a plain
    `python`/`py` over versioned binaries only when it already qualifies."""
    if target_os == "windows":
        candidates = [["py", "-3.13"], ["py", "-3.12"], ["py", "-3.11"], ["py"], ["python"]]
    else:
        candidates = [["python3.13"], ["python3.12"], ["python3.11"], ["python3"], ["python"]]
    for cmd_prefix in candidates:
        if not have(cmd_prefix[0]):
            continue
        version = python_version(cmd_prefix)
        if version and version >= MIN_PYTHON:
            return cmd_prefix, version
    return None, None


def venv_python_path(venv_dir, target_os):
    if target_os == "windows":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def install_missing_deps(target_os, missing):
    if target_os == "windows":
        winget_ids = {
            "adb": "Google.PlatformTools",
            "node": "OpenJS.NodeJS.LTS",
        }
        for tool in ("adb", "node"):
            if tool in missing:
                run(["winget", "install", "--id", winget_ids[tool], "-e"])
        if "redis" in missing:
            if have("docker"):
                run(["docker", "run", "-d", "--name", "autoperf-redis",
                     "-p", "6379:6379", "redis:7-alpine"])
            else:
                warn("Docker not found -- install Docker Desktop, then re-run, "
                     "or start Redis some other way (see docs/INSTALL.md).")
    elif target_os == "macos":
        if not have("brew"):
            warn("Homebrew not found -- install it from https://brew.sh first.")
            return
        if "adb" in missing:
            run(["brew", "install", "android-platform-tools"])
        if "node" in missing:
            run(["brew", "install", "node"])
        if "redis" in missing:
            run(["brew", "install", "redis"])
            run(["brew", "services", "start", "redis"])
    else:  # linux / wsl
        if not have("apt-get"):
            warn("No apt-get found -- this script only automates Debian/Ubuntu "
                 "derivatives. Install adb/nodejs/redis with your distro's "
                 "package manager, then re-run without --install-deps.")
            return
        run(["sudo", "apt-get", "update"])
        if "adb" in missing:
            run(["sudo", "apt-get", "install", "-y", "android-tools-adb"])
        if "node" in missing:
            run(["sudo", "apt-get", "install", "-y", "nodejs", "npm"])
        if "redis" in missing:
            run(["sudo", "apt-get", "install", "-y", "redis-server"])
            run(["sudo", "service", "redis-server", "start"])


def redis_reachable(host="localhost", port=6379, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-deps", action="store_true",
                         help="also install missing adb/node/redis automatically")
    args = parser.parse_args()

    target_os = detect_os()
    step("Detected platform: " + target_os)

    step("Python virtual environment (./venv)")
    venv_dir = REPO_ROOT / "venv"
    venv_python = venv_python_path(venv_dir, target_os)
    if not venv_python.exists():
        cmd_prefix, version = find_python(target_os)
        if not cmd_prefix:
            warn("No Python %d.%d+ found on PATH." % MIN_PYTHON)
            if target_os == "windows":
                warn("  winget install Python.Python.3.12")
            elif target_os == "macos":
                warn("  brew install python@3.12")
            else:
                warn("  sudo apt-get install -y python3.12 python3.12-venv")
            sys.exit(1)
        ok("Using %s (Python %d.%d)" % (" ".join(cmd_prefix), *version))
        run(cmd_prefix + ["-m", "venv", str(venv_dir)])
    else:
        ok("venv already exists")

    step("Installing AutoPerf (editable, all extras)")
    run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(venv_python), "-m", "pip", "install", "-e",
         ".[dashboard,worker,livescreen,dev]"], cwd=str(REPO_ROOT))
    ok("Python package installed")

    step("Checking system tools (adb, node, redis)")
    missing = []
    if have("adb"):
        ok("adb found: " + shutil.which("adb"))
    else:
        missing.append("adb")
        warn("adb not found on PATH")
    if have("node"):
        ok("node found: " + shutil.which("node"))
    else:
        missing.append("node")
        warn("node not found on PATH (needed for the dashboard frontend)")
    if redis_reachable():
        ok("Something is already listening on localhost:6379")
    else:
        missing.append("redis")
        warn("no Redis detected on localhost:6379 (needed for the dashboard)")

    if missing and args.install_deps:
        step("Installing missing dependencies: " + ", ".join(missing))
        install_missing_deps(target_os, missing)
    elif missing:
        step("Missing dependencies: " + ", ".join(missing))
        warn("Re-run with --install-deps to install them automatically,")
        warn("or see docs/INSTALL.md for manual install commands.")

    if have("node"):
        frontend_dir = REPO_ROOT / "webapp" / "frontend"
        if frontend_dir.exists():
            step("Installing frontend npm dependencies")
            run(["npm", "install"], cwd=str(frontend_dir))

    if target_os == "wsl":
        step("WSL note")
        warn("WSL2 has no native USB passthrough, so a phone plugged in via")
        warn("USB usually will NOT show up in `adb devices` here. Prefer")
        warn("ADB over WiFi (adb pair / adb connect) instead of USB passthrough")
        warn("tools -- see the WSL section of docs/INSTALL.md.")

    step("Done")
    print("Next steps:")
    print("  adb devices")
    print("  " + str(venv_python) + " -m autoperf devices")
    print("See docs/INSTALL.md for how to start the dashboard, Celery worker, "
          "and frontend.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print("\nCommand failed: " + " ".join(exc.cmd), file=sys.stderr)
        sys.exit(exc.returncode)
