#!/usr/bin/env python3
"""Starts every process the AutoPerf dashboard needs, in one terminal.

`docs/INSTALL.md` asks for three terminals plus a Redis you started some
other time; this script is that same sequence with the ordering, the health
checks and the shutdown written down once:

    Redis broker  ->  Django API :8000  ->  Celery worker(s)
                  ->  live-screen :8100  ->  Vite frontend :5173

Each service is health-checked before the next one starts, so a failure is
reported against the service that actually failed rather than as a blank
page three steps later. Ctrl+C stops all of them, children first.

    .\\venv\\Scripts\\python.exe scripts\\StartServices.py     # Windows
    ./venv/bin/python scripts/StartServices.py              # macOS/Linux

Run it with the project's own venv Python -- it launches `manage.py`,
`scripts/start-worker.py` and `livescreen.server` with `sys.executable`, so
whichever interpreter starts this one is the interpreter they all get.

Skip what you don't need: --no-frontend (API only), --no-worker (browse
history without running anything), --no-livescreen, --no-redis.

## The WSL Redis trap this script exists to work around

On Windows the recommended broker is Redis inside WSL (see setup.py's
`ensure_wsl_redis`). Ubuntu's packaged redis-server binds **127.0.0.1
inside the distro**, and WSL 2's localhostForwarding does not reliably
bridge a loopback-only listener -- so `redis-cli ping` answers PONG inside
WSL while Windows gets WinError 10061 from the very same port, and Celery
sits in a "Cannot connect to redis://localhost:6379" retry loop forever.
Measured on this machine: reachable once, refused a minute later.

So reachability is checked **from Windows**, not from inside WSL, and when
the distro's own Redis fails that check this script starts a second
instance bound to 0.0.0.0 on `--redis-fallback-port` (default 6380) and
exports `AUTOPERF_CELERY_BROKER_URL` pointing at it. Django and the worker
both read that variable (`webapp/config/settings.py`), and they only agree
because they are launched from here with the same environment -- which is
the other reason this script exists.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBAPP_DIR = REPO_ROOT / "webapp"
FRONTEND_DIR = WEBAPP_DIR / "frontend"

DEFAULT_API_PORT = 8000
DEFAULT_LIVESCREEN_PORT = 8100  # hard-coded in frontend/src/composables/useDeviceScreen.js
DEFAULT_FRONTEND_PORT = 5173    # Vite's default; the API proxy target is in vite.config.js
DEFAULT_REDIS_PORT = 6379
DEFAULT_REDIS_FALLBACK_PORT = 6380


def step(msg):
    print("\n==> " + msg, flush=True)


def ok(msg):
    print("    " + msg, flush=True)


def warn(msg):
    print("    ! " + msg, flush=True)


def is_windows():
    return platform.system() == "Windows"


# --- health checks ----------------------------------------------------------
#
# Two kinds, and the difference matters: a TCP connect proves something is
# listening, an HTTP 200 proves the app behind it actually serves. Django
# accepts a connection well before it has finished importing settings, so the
# API is checked by requesting a real endpoint.

def port_open(host, port, timeout=1.5):
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def port_in_use(host, port):
    """Is something listening, asked without connecting to it.

    The live-screen server speaks WebSocket and nothing else: a bare TCP
    connect that opens and closes makes it log a full EOFError traceback for
    a failed opening handshake, so a health check written the obvious way
    fills the log with an error it caused itself. Trying to *bind* the port
    answers the same question and never touches the process.
    """
    with socket.socket() as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def http_ok(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError):
        return False


def wait_until(check, seconds, poll=0.5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(poll)
    return check()


# --- Redis ------------------------------------------------------------------

def wsl_available():
    return is_windows() and shutil.which("wsl.exe") is not None


def start_wsl_redis(port, bind_all):
    """Start redis-server inside WSL. Returns True if the command succeeded.

    `--save ''` disables RDB snapshots: this is a job broker, and a queue that
    survives a reboot is not something anything here relies on -- Storage's
    `cancel_requested` flag is what stops runs, not Celery's queue state.
    """
    command = ["redis-server", "--daemonize", "yes", "--port", str(port), "--save", ""]
    if bind_all:
        command += ["--bind", "0.0.0.0", "--protected-mode", "no"]
    result = subprocess.run(
        ["wsl.exe", "-e", "sh", "-c", " ".join(command[:1] + [_shell_quote(a) for a in command[1:]])],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _shell_quote(arg):
    return "''" if arg == "" else arg


def ensure_redis(args):
    """Make a broker reachable *from this OS* and return its URL.

    Returns None when every option failed; the caller decides whether that is
    fatal (it is only fatal for the worker -- Django and the frontend serve
    history perfectly well against a dead broker, and /queue says so plainly).
    """
    host = args.redis_host
    if port_open(host, args.redis_port):
        ok("Redis already reachable at %s:%d" % (host, args.redis_port))
        return "redis://%s:%d/0" % (host, args.redis_port)

    if shutil.which("redis-server"):
        ok("Starting local redis-server on port %d" % args.redis_port)
        subprocess.Popen(
            ["redis-server", "--port", str(args.redis_port), "--save", ""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if wait_until(lambda: port_open(host, args.redis_port), 10):
            return "redis://%s:%d/0" % (host, args.redis_port)
        warn("redis-server started but nothing is listening on %d" % args.redis_port)

    if shutil.which("docker"):
        ok("Starting the autoperf-redis container")
        subprocess.run(["docker", "start", "autoperf-redis"], capture_output=True)
        if not port_open(host, args.redis_port):
            subprocess.run([
                "docker", "run", "-d", "--name", "autoperf-redis",
                "--restart", "unless-stopped",
                "-p", "%d:6379" % args.redis_port, "redis:7-alpine",
            ], capture_output=True)
        if wait_until(lambda: port_open(host, args.redis_port), 20):
            return "redis://%s:%d/0" % (host, args.redis_port)

    if wsl_available():
        # First try the distro's own service on the standard port. If Windows
        # still cannot reach it, that is the loopback-bind trap in this
        # module's docstring -- not a Redis that failed to start.
        ok("Starting Redis inside WSL")
        start_wsl_redis(args.redis_port, bind_all=False)
        if wait_until(lambda: port_open(host, args.redis_port), 8):
            return "redis://%s:%d/0" % (host, args.redis_port)

        warn("Redis answers inside WSL but Windows cannot reach %s:%d"
             % (host, args.redis_port))
        warn("(WSL's redis-server binds 127.0.0.1 inside the distro; "
             "localhostForwarding does not bridge that reliably)")
        ok("Starting a second Redis on %d, bound to 0.0.0.0" % args.redis_fallback_port)
        start_wsl_redis(args.redis_fallback_port, bind_all=True)
        for candidate in (host, wsl_ip()):
            if candidate and wait_until(
                lambda c=candidate: port_open(c, args.redis_fallback_port), 8
            ):
                ok("Broker reachable at %s:%d" % (candidate, args.redis_fallback_port))
                return "redis://%s:%d/0" % (candidate, args.redis_fallback_port)

    return None


def wsl_ip():
    """The WSL VM's own address, for when localhost forwarding is not working."""
    try:
        result = subprocess.run(
            ["wsl.exe", "-e", "sh", "-c", "hostname -I"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.split()[0] if result.stdout.split() else None


# --- child processes --------------------------------------------------------

class Service:
    """One child process, its log tag, and how to tell whether it came up."""

    def __init__(self, name, command, cwd, env, ready, ready_seconds=45, required=True):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.env = env
        self.ready = ready
        self.ready_seconds = ready_seconds
        self.required = required
        self.process = None

    def start(self):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0
        self.process = subprocess.Popen(
            self.command, cwd=str(self.cwd), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=creationflags,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.process.stdout:
            # Vite's banner contains U+279C, which a cp950 console cannot
            # encode -- and an unhandled UnicodeEncodeError here kills the
            # thread, so the service keeps running while its log silently
            # stops. main() widens stdout's error handling for exactly this;
            # the guard is the second line of defence, because losing a log
            # relay must never be something you find out about later.
            try:
                print("[%s] %s" % (self.name, line.rstrip()), flush=True)
            except (UnicodeEncodeError, ValueError):
                pass

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if not self.alive():
            return
        if is_windows():
            # `npm run dev` is a .cmd shim that spawns node as a *child*:
            # terminating the shim alone leaves Vite holding port 5173, and
            # the next start of this script then fails on a port that nothing
            # visible owns. /T kills the tree. (Observed exactly this.)
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                capture_output=True,
            )
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def npm_executable():
    return shutil.which("npm.cmd") or shutil.which("npm")


def build_services(args, env):
    services = []
    python = sys.executable

    services.append(Service(
        "api",
        # --noreload: the autoreloader forks a second process this script would
        # not own, so Ctrl+C would leave port 8000 held by an orphan.
        [python, str(WEBAPP_DIR / "manage.py"), "runserver", str(args.api_port), "--noreload"],
        cwd=REPO_ROOT, env=env,
        ready=lambda: http_ok("http://127.0.0.1:%d/api/devices" % args.api_port),
    ))

    if not args.no_worker:
        command = [python, str(REPO_ROOT / "scripts" / "start-worker.py")]
        if args.concurrency:
            command += ["--concurrency", str(args.concurrency)]
        services.append(Service(
            "worker", command, cwd=REPO_ROOT, env=env,
            # Asks the API, not Celery: worker_online there is what the Task
            # Queue page shows, so this checks the thing a user will look at.
            ready=lambda: queue_reports_worker(args.api_port),
            ready_seconds=60,
            # Not required: a dashboard with no worker still serves history,
            # and /queue reports the absence honestly.
            required=False,
        ))

    if not args.no_livescreen:
        services.append(Service(
            "livescreen",
            [python, "-m", "livescreen.server", "--port", str(args.livescreen_port)],
            cwd=WEBAPP_DIR, env=env,
            ready=lambda: port_in_use("127.0.0.1", args.livescreen_port),
            required=False,
        ))

    if not args.no_frontend:
        npm = npm_executable()
        if npm is None:
            warn("npm is not on PATH -- skipping the frontend. Install Node, or "
                 "pass --no-frontend to stop being told about it.")
        else:
            services.append(Service(
                "frontend",
                [npm, "run", "dev", "--", "--port", str(args.frontend_port)],
                cwd=FRONTEND_DIR, env=env,
                ready=lambda: http_ok("http://127.0.0.1:%d/" % args.frontend_port),
                ready_seconds=90,
            ))

    return services


def queue_reports_worker(api_port):
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/api/queue" % api_port, timeout=5
        ) as response:
            import json
            return bool(json.loads(response.read()).get("worker_online"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ensure_frontend_deps():
    if (FRONTEND_DIR / "node_modules").is_dir():
        return True
    npm = npm_executable()
    if npm is None:
        return False
    step("Installing frontend dependencies (first run only)")
    return subprocess.run([npm, "install"], cwd=str(FRONTEND_DIR)).returncode == 0


def preflight_imports():
    """Fail here, with the fix, rather than inside a child's traceback."""
    try:
        import celery  # noqa: F401
        import django  # noqa: F401
    except ImportError:
        warn("django/celery aren't importable from %s." % sys.executable)
        warn("Run this with the project's venv Python, and install the extras:")
        warn("  " + sys.executable + " -m pip install -e .[dashboard,worker,livescreen]")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--livescreen-port", type=int, default=DEFAULT_LIVESCREEN_PORT)
    parser.add_argument("--redis-host", default="127.0.0.1")
    parser.add_argument("--redis-port", type=int, default=DEFAULT_REDIS_PORT)
    parser.add_argument("--redis-fallback-port", type=int, default=DEFAULT_REDIS_FALLBACK_PORT,
                        help="port for a 0.0.0.0-bound Redis when the default one "
                             "is unreachable from this OS (see module docstring)")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="passed through to scripts/start-worker.py")
    parser.add_argument("--no-redis", action="store_true", help="assume a broker is already running")
    parser.add_argument("--no-worker", action="store_true")
    parser.add_argument("--no-livescreen", action="store_true")
    parser.add_argument("--no-frontend", action="store_true")
    args = parser.parse_args()

    # Children's logs are relayed verbatim, and they contain characters the
    # console codepage may not have (Vite's arrow on a cp950 terminal). Losing
    # a character is acceptable; losing the log relay is not.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

    preflight_imports()

    env = os.environ.copy()
    # Every child resolves `config.settings` / `livescreen` out of webapp/,
    # and each one would find it anyway (a script's own directory, and `-m`'s
    # cwd, both land on sys.path). Stated once here so none of them depends on
    # which directory it happened to be started from.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(WEBAPP_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env.setdefault("PYTHONUNBUFFERED", "1")

    if args.no_redis:
        broker = env.get("AUTOPERF_CELERY_BROKER_URL")
        ok("Skipping Redis startup (--no-redis)")
    else:
        step("Redis broker")
        broker = ensure_redis(args)

    if broker:
        # The whole point of starting everything from one process: Django and
        # the worker cannot disagree about where the broker is.
        env["AUTOPERF_CELERY_BROKER_URL"] = broker
        ok("Broker: " + broker)
    elif not args.no_redis:
        warn("No reachable Redis. The dashboard will still serve history and "
             "the Task Queue page will say the broker is unreachable, but "
             "starting a run from the UI will not work.")
        args.no_worker = True

    if not args.no_frontend and not ensure_frontend_deps():
        warn("Frontend dependencies are missing and npm install did not succeed "
             "-- continuing without the frontend.")
        args.no_frontend = True

    services = build_services(args, env)
    started = []
    try:
        for service in services:
            step("Starting %s" % service.name)
            ok("$ " + " ".join(str(part) for part in service.command))
            service.start()
            started.append(service)
            if wait_until(service.ready, service.ready_seconds):
                ok("%s is up" % service.name)
                continue
            if service.alive() and not service.required:
                warn("%s did not report ready in %ds -- continuing without it"
                     % (service.name, service.ready_seconds))
                continue
            warn("%s failed to start" % service.name)
            return shutdown(started, code=1)

        step("Ready")
        ok("Dashboard      http://127.0.0.1:%d" % args.frontend_port)
        ok("API            http://127.0.0.1:%d/api/devices" % args.api_port)
        ok("Live screen    ws://127.0.0.1:%d" % args.livescreen_port)
        ok("Ctrl+C stops everything.")

        reported = set()
        while True:
            time.sleep(1)
            for service in started:
                if service.alive() or service.name in reported:
                    continue
                warn("%s exited (code %s)" % (service.name, service.process.returncode))
                reported.add(service.name)
                # Only a required service ending ends the session. An optional
                # one exiting used to bring the whole stack down with it --
                # which is how asking Celery to shut down warmly (the safe way
                # to requeue its work before a restart) also killed the API
                # and the frontend nobody had asked to stop.
                if service.required:
                    return shutdown(started, code=1)
    except KeyboardInterrupt:
        print()
        return shutdown(started, code=0)


def shutdown(services, code):
    step("Stopping services")
    # Reverse order: the frontend and the worker are the ones users have open
    # connections to, and the API is what they would notice going first.
    for service in reversed(services):
        if service.alive():
            ok("stopping %s" % service.name)
            service.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
