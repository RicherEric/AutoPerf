#!/usr/bin/env python3
"""Starts the AutoPerf dashboard's Celery worker(s), sized by default to
min(cpu_count, connected_devices + 1) -- enough to drive every attached
device at once, plus one, and never more than the machine has cores.

The `+ 1` is the part worth explaining, because the sizing used to be exactly
`min(cpu_count, connected_devices)`. That was right when a run was the only
kind of device job: one worker per device meant every device could be driven
and nothing else could be. It stopped being right when preflight became
queueable from the dashboard. Preflight is a second kind of device job -- it
holds a phone for as long as three minutes and produces no measurements --
so on a one-phone laptop the old formula gave exactly one worker, and that
worker being inside a preflight meant *every* queued job waited on it,
including jobs for a device that was completely idle.

What keeps two things off the same phone is not the worker count. It is
Storage.try_start_run() / try_start_preflight(), a single atomic UPDATE that
refuses either job while the other holds that serial -- see DeviceBusyError
in autoperf.runner, and the preflight task in dashboard.tasks. A job that
loses that race retries every couple of seconds instead of racing two adb
sessions against one device. Once exclusion lives in the database, capping
workers at the device count only buys head-of-line blocking.

Must be run with the project's own venv Python (it imports `autoperf.adb`
directly to count devices, and needs the venv's `celery` executable):

    ./venv/bin/python scripts/start-worker.py            # macOS/Linux/WSL
    .\\venv\\Scripts\\python.exe scripts\\start-worker.py   # Windows

Windows is forced onto Celery's `--pool=solo` (single task at a time per
process) because TestRunner.run() calls signal.signal(SIGINT, ...), which
only works on a process's main thread -- solo is the only pool where a
task runs directly on the worker's own main thread on Windows. So on
Windows, N-way parallelism means launching N separate `--pool=solo`
processes; on macOS/Linux/WSL, prefork's own `--concurrency=N` already
does this within one process (each forked child keeps its own main thread,
so the SIGINT handling constraint still holds).

Usage:
    python scripts/start-worker.py [--concurrency N] [--loglevel info]
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBAPP_DIR = REPO_ROOT / "webapp"


def step(msg):
    print("\n==> " + msg)


def ok(msg):
    print("    " + msg)


def warn(msg):
    print("    ! " + msg)


def is_windows():
    return platform.system() == "Windows"


def count_connected_devices():
    try:
        from autoperf.adb import AdbClient, AdbError
    except ImportError:
        warn("autoperf isn't importable from this Python -- run this script with "
             "the project's venv Python, e.g.:")
        warn("  ./venv/bin/python scripts/start-worker.py")
        sys.exit(1)
    try:
        devices = AdbClient().devices()
    except AdbError as exc:
        warn("Couldn't query `adb devices` (%s)." % exc)
        return 0
    return sum(1 for d in devices if d.state == "device")


# One slot per attached device, plus one so a long job on one device (a
# preflight is minutes) cannot stall the queue for every other device.
SPARE_WORKERS = 1


def choose_concurrency(explicit):
    if explicit:
        ok("Using explicit --concurrency=%d" % explicit)
        return explicit
    cpu_n = os.cpu_count() or 1
    device_n = count_connected_devices()
    if device_n == 0:
        warn("No adb devices currently connected -- defaulting to 2 workers.")
        warn("Plug in device(s) and re-run, or pass --concurrency N to override.")
        return min(cpu_n, 2)
    n = max(1, min(cpu_n, device_n + SPARE_WORKERS))
    ok("CPU cores: %d, connected devices: %d -> concurrency=%d" % (cpu_n, device_n, n))
    return n


def celery_executable():
    name = "celery.exe" if is_windows() else "celery"
    candidate = Path(sys.executable).parent / name
    if not candidate.exists():
        warn("Couldn't find %s next to %s." % (name, sys.executable))
        warn("Install the dashboard/worker extras first:")
        warn("  " + sys.executable + " -m pip install -e .[dashboard,worker]")
        sys.exit(1)
    return str(candidate)


def run_posix(celery, n, loglevel):
    cmd = [celery, "-A", "config", "worker", "--concurrency=%d" % n, "-l", loglevel]
    step("Starting Celery worker (prefork, concurrency=%d)" % n)
    ok("$ " + " ".join(cmd))
    os.execvp(celery, cmd)


def run_windows(celery, n, loglevel):
    if n == 1:
        step("Starting 1 --pool=solo worker process")
        cmd = [celery, "-A", "config", "worker", "--pool=solo", "-l", loglevel]
        ok("$ " + " ".join(cmd))
        os.execvp(celery, cmd)
        return

    step("Starting %d --pool=solo worker processes" % n)
    procs = []
    for k in range(1, n + 1):
        cmd = [celery, "-A", "config", "worker", "--pool=solo", "-n", "worker%d@%%h" % k, "-l", loglevel]
        ok("$ " + " ".join(cmd))
        procs.append(subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP))
    try:
        for proc in procs:
            proc.wait()
    except KeyboardInterrupt:
        step("Stopping workers")
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--concurrency", type=int, default=None,
                         help="override the auto-detected worker count")
    parser.add_argument("--loglevel", default="info", help="Celery -l loglevel (default: info)")
    args = parser.parse_args()

    step("Sizing worker concurrency")
    n = choose_concurrency(args.concurrency)

    celery = celery_executable()
    os.chdir(str(WEBAPP_DIR))

    if is_windows():
        run_windows(celery, n, args.loglevel)
    else:
        run_posix(celery, n, args.loglevel)


if __name__ == "__main__":
    main()
