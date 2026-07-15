from __future__ import annotations

import multiprocessing as mp
import uuid

from django.conf import settings

from autoperf.storage import Storage
from autoperf.workers import _device_worker


def get_storage() -> Storage:
    return Storage(settings.AUTOPERF_DB_PATH)


def trigger_run(storage: Storage, serial: str, duration: float) -> str:
    """Start a test run as an independent OS process and return immediately.

    TestRunner.run() installs a SIGINT handler, which only works on the main
    thread of the main interpreter -- a plain threading.Thread here would
    crash after the run row is already marked "running", leaking a stuck row
    and an orphaned BatchWriter thread. Spawning via multiprocessing (the same
    mechanism autoperf.workers.DeviceSupervisor already uses for run-many)
    puts TestRunner.run() on the main thread of a fresh process instead, so
    this bug class doesn't apply, and the run also survives the dev server
    being restarted.
    """
    run_id = uuid.uuid4().hex
    storage.create_run(run_id, serial)
    ctx = mp.get_context("spawn")
    heartbeat = ctx.Queue()
    ctx.Process(
        target=_device_worker,
        args=(storage.path, serial, duration, run_id, heartbeat, 1.0),
        name=f"autoperf-dashboard-{serial}",
    ).start()
    return run_id
