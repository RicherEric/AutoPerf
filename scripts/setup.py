#!/usr/bin/env python3
"""Idempotent installer for the complete AutoPerf development service.

Creates the Python environment, installs all backend/frontend dependencies,
prepares runtime directories, optionally installs system tools, and verifies
the resulting installation.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBAPP = ROOT / "webapp"
FRONTEND = WEBAPP / "frontend"
MIN_PYTHON = (3, 11)
MIN_NODE = (18, 0)


def step(message: str) -> None:
    print(f"\n==> {message}")


def ok(message: str) -> None:
    print(f"    {message}")


def warn(message: str) -> None:
    print(f"    ! {message}")


def run(command: list[str | Path], **kwargs) -> subprocess.CompletedProcess:
    command = [str(part) for part in command]
    print("    $ " + " ".join(command))
    return subprocess.run(command, check=True, **kwargs)


def have(command: str) -> bool:
    return shutil.which(command) is not None


def command_version(command: list[str], pattern: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10,
        )
        match = re.search(pattern, result.stdout.strip())
        return (int(match.group(1)), int(match.group(2))) if result.returncode == 0 and match else None
    except (OSError, subprocess.SubprocessError):
        return None


def refresh_windows_path() -> None:
    """Import newly installed machine/user PATH entries into this process."""
    if platform.system() != "Windows":
        return
    try:
        import winreg

        paths = [os.environ.get("PATH", "")]
        locations = (
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        )
        for hive, key_name in locations:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                paths.append(os.path.expandvars(value))
        entries = []
        seen = set()
        for entry in os.pathsep.join(paths).split(os.pathsep):
            normalized = os.path.normcase(entry.strip())
            if entry.strip() and normalized not in seen:
                entries.append(entry.strip())
                seen.add(normalized)
        os.environ["PATH"] = os.pathsep.join(entries)
    except OSError as exc:
        warn(f"Could not refresh Windows PATH: {exc}")


def detect_os() -> str:
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        release = platform.uname().release.lower()
        return "wsl" if "microsoft" in release or os.environ.get("WSL_DISTRO_NAME") else "linux"
    raise RuntimeError(f"Unsupported platform: {system}")


def python_version(prefix: list[str]) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            prefix + ["-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            capture_output=True, text=True, timeout=10,
        )
        major, minor = result.stdout.strip().split(".")
        return (int(major), int(minor)) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_python(target_os: str) -> tuple[list[str] | None, tuple[int, int] | None]:
    candidates = (
        [["py", "-3"], ["python"]]
        if target_os == "windows"
        else [["python3"], ["python"]]
    )
    for prefix in candidates:
        version = python_version(prefix)
        if version and version >= MIN_PYTHON:
            return prefix, version
    return None, None


def redis_reachable() -> bool:
    try:
        with socket.create_connection(("localhost", 6379), timeout=2) as connection:
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            return connection.recv(64).startswith(b"+PONG")
    except OSError:
        return False


def ensure_docker_redis() -> None:
    exists = subprocess.run(
        ["docker", "inspect", "autoperf-redis"], capture_output=True,
    ).returncode == 0
    if exists:
        running = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "autoperf-redis"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if running != "true":
            run(["docker", "start", "autoperf-redis"])
        else:
            ok("Redis container is already running")
    else:
        run([
            "docker", "run", "-d", "--name", "autoperf-redis",
            "--restart", "unless-stopped", "-p", "6379:6379", "redis:7-alpine",
        ])


def wsl_distributions() -> list[str]:
    """Return installed WSL distributions without depending on localized output."""
    result = subprocess.run(
        ["wsl.exe", "--list", "--quiet"],
        capture_output=True, text=True, encoding="utf-16-le", errors="replace",
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).replace("\x00", "").strip()
        if "E_ACCESSDENIED" in details:
            raise RuntimeError(
                "WSL is installed but its service is not ready (E_ACCESSDENIED). "
                "Restart Windows, then re-run this installer."
            )
        raise RuntimeError(
            "Unable to query WSL distributions"
            + (f": {details}" if details else ". Restart Windows and try again.")
        )
    return [
        line.replace("\x00", "").strip()
        for line in result.stdout.splitlines()
        if line.replace("\x00", "").strip()
    ]


def select_wsl_distribution(distributions: list[str], requested: str | None) -> str:
    if requested:
        matches = [item for item in distributions if item.casefold() == requested.casefold()]
        if not matches:
            raise RuntimeError(
                f"WSL distribution {requested!r} is not installed. Available: "
                + ", ".join(distributions)
            )
        return matches[0]
    preferred = [
        item for item in distributions
        if "ubuntu" in item.casefold() or "debian" in item.casefold()
    ]
    if not preferred:
        raise RuntimeError(
            "No Ubuntu/Debian WSL distribution is installed. Install Ubuntu with "
            "`wsl --install -d Ubuntu`, or select a compatible distro with --wsl-distro."
        )
    return preferred[0]


def ensure_wsl_redis(requested_distro: str | None = None) -> None:
    if not have("wsl.exe"):
        raise RuntimeError(
            "WSL is unavailable. Run `wsl --install -d Ubuntu` as Administrator, "
            "restart Windows, then re-run this installer."
        )

    distributions = wsl_distributions()
    if not distributions:
        step("Installing WSL 2 and Ubuntu")
        result = subprocess.run(["wsl.exe", "--install", "-d", "Ubuntu"])
        if result.returncode != 0:
            raise RuntimeError(
                "WSL installation requires an elevated PowerShell window. Run "
                "`wsl --install -d Ubuntu`, restart Windows if requested, then "
                "re-run this installer."
            )
        raise RuntimeError(
            "WSL/Ubuntu was enabled. Restart Windows if requested, open Ubuntu "
            "once to finish initialization, then re-run this installer."
        )

    distro = select_wsl_distribution(distributions, requested_distro)
    step(f"Installing Redis inside WSL distribution: {distro}")
    linux_command = (
        "set -e; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update; "
        "apt-get install -y redis-server; "
        "(systemctl enable --now redis-server 2>/dev/null || "
        "service redis-server start); "
        "redis-cli ping"
    )
    run(["wsl.exe", "-d", distro, "-u", "root", "--", "sh", "-lc", linux_command])
    if not redis_reachable():
        raise RuntimeError(
            "Redis is running in WSL but Windows cannot reach localhost:6379. "
            "Ensure `.wslconfig` has `localhostForwarding=true`, run "
            "`wsl --shutdown`, then re-run this installer."
        )
    ok(f"Redis is reachable from Windows via WSL ({distro})")


def install_system_deps(
    target_os: str, missing: list[str], redis_backend: str = "auto",
    wsl_distro: str | None = None,
) -> None:
    if target_os == "windows":
        ids = {"adb": "Google.PlatformTools", "node": "OpenJS.NodeJS.LTS", "ffmpeg": "Gyan.FFmpeg"}
        for tool in ("adb", "node", "ffmpeg"):
            if tool in missing:
                if not have("winget"):
                    raise RuntimeError(f"winget is required to install {tool}")
                run(["winget", "install", "--id", ids[tool], "-e"])
                refresh_windows_path()
        if "redis" in missing:
            if redis_backend == "docker":
                if not have("docker"):
                    raise RuntimeError("Docker is not installed; use --redis-backend wsl")
                ensure_docker_redis()
            else:
                ensure_wsl_redis(wsl_distro)
    elif target_os == "macos":
        if not have("brew"):
            raise RuntimeError("Homebrew is required; install it from https://brew.sh")
        formulas = {"adb": "android-platform-tools", "node": "node", "ffmpeg": "ffmpeg", "redis": "redis"}
        for tool in missing:
            run(["brew", "install", formulas[tool]])
        if "redis" in missing:
            run(["brew", "services", "start", "redis"])
    else:
        if not have("apt-get"):
            raise RuntimeError("Automatic Linux installation currently requires apt-get")
        privilege = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo"]
        if privilege and not have("sudo"):
            raise RuntimeError("sudo is required to install Linux system dependencies")
        run([*privilege, "apt-get", "update"])
        packages = {
            "adb": ["android-tools-adb"], "node": ["nodejs", "npm"],
            "ffmpeg": ["ffmpeg"], "redis": ["redis-server"],
        }
        selected = [package for tool in missing for package in packages[tool]]
        if selected:
            run([*privilege, "apt-get", "install", "-y", *selected])
        if "redis" in missing:
            run([*privilege, "service", "redis-server", "start"])


def check_system_tools() -> list[str]:
    step("Checking service prerequisites")
    missing = []
    for command, label in (("adb", "Android platform-tools"), ("ffmpeg", "ffmpeg")):
        location = shutil.which(command)
        if location:
            ok(f"{label}: {location}")
        else:
            missing.append(command)
            warn(f"{label} not found")
    node = shutil.which("node")
    node_version = command_version([node or "node", "--version"], r"v?(\d+)\.(\d+)")
    if node and node_version and node_version >= MIN_NODE:
        ok(f"Node.js: {node} ({node_version[0]}.{node_version[1]})")
    else:
        missing.append("node")
        found = f"{node_version[0]}.{node_version[1]}" if node_version else "unavailable"
        warn(f"Node.js 18+ required; found {found}")
    if redis_reachable():
        ok("Redis: localhost:6379")
    else:
        missing.append("redis")
        warn("Redis is not reachable on localhost:6379")
    return missing


def prepare_python(target_os: str) -> Path:
    step("Preparing Python virtual environment")
    venv = ROOT / "venv"
    python = venv / ("Scripts/python.exe" if target_os == "windows" else "bin/python")
    if not python.exists():
        prefix, version = find_python(target_os)
        if not prefix:
            raise RuntimeError("Python 3.11 or newer is required")
        ok(f"Using {' '.join(prefix)} ({version[0]}.{version[1]})")
        run(prefix + ["-m", "venv", venv])
    else:
        version = python_version([str(python)])
        if not version or version < MIN_PYTHON:
            raise RuntimeError(f"Existing venv requires Python 3.11+; found {version}")
        ok(f"Existing venv uses Python {version[0]}.{version[1]}")

    step("Installing backend, worker, live-screen, and test dependencies")
    run([python, "-m", "pip", "install", "--upgrade", "pip"])
    run([python, "-m", "pip", "install", "-r", "requirements.txt"], cwd=ROOT)
    return python


def prepare_frontend() -> bool:
    npm = shutil.which("npm")
    if not npm:
        warn("npm unavailable; frontend installation skipped")
        return False
    step("Installing locked dashboard frontend dependencies")
    run([npm, "ci"] if (FRONTEND / "package-lock.json").exists() else [npm, "install"], cwd=FRONTEND)
    return True


def prepare_runtime() -> None:
    step("Preparing runtime directories")
    for directory in (ROOT / "artifacts", WEBAPP / "recordings"):
        directory.mkdir(parents=True, exist_ok=True)
        ok(str(directory.relative_to(ROOT)))


def verify(python: Path, frontend_ready: bool) -> None:
    step("Verifying backend services")
    run([
        python, "-c",
        "import autoperf, celery, django, redis, websockets; print('    imports: OK')",
    ], cwd=ROOT)
    run([python, "manage.py", "check"], cwd=WEBAPP)
    run([python, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT)
    if frontend_ready:
        step("Building dashboard frontend")
        run([shutil.which("npm") or "npm", "run", "build"], cwd=FRONTEND)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-deps", action="store_true", help="install missing system tools")
    parser.add_argument(
        "--redis-backend", choices=("auto", "wsl", "docker"), default="auto",
        help="Redis runtime on Windows (default: WSL; Docker is opt-in)",
    )
    parser.add_argument(
        "--wsl-distro",
        help="Ubuntu/Debian WSL distribution used for Redis (default: auto-detect)",
    )
    parser.add_argument("--skip-verify", action="store_true", help="skip tests, Django check, and frontend build")
    args = parser.parse_args()

    target_os = detect_os()
    step(f"Detected platform: {target_os}")
    missing = check_system_tools()
    if missing and args.install_deps:
        step("Installing missing system dependencies: " + ", ".join(missing))
        install_system_deps(target_os, missing, args.redis_backend, args.wsl_distro)
        refresh_windows_path()
        remaining = check_system_tools()
        if remaining:
            raise RuntimeError(
                "System dependencies are still unavailable after installation: "
                + ", ".join(remaining)
                + ". Open a new terminal and re-run the installer."
            )
    elif missing:
        warn("Re-run with --install-deps to install missing system dependencies.")

    python = prepare_python(target_os)
    frontend_ready = prepare_frontend()
    prepare_runtime()
    if not args.skip_verify:
        verify(python, frontend_ready)

    step("Installation complete")
    print("Start everything, in one terminal:")
    print(f"  {python} scripts/StartServices.py")
    print()
    print("Or start each service in its own terminal:")
    print(f"  {python} webapp/manage.py runserver 8000")
    print(f"  {python} scripts/start-worker.py")
    print("  cd webapp/frontend && npm run dev")
    print(f"  cd webapp && {python} -m livescreen.server --port 8100")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"\nInstallation failed: {exc}", file=sys.stderr)
        sys.exit(exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1)
