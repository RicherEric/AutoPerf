from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time
import uuid
from dataclasses import dataclass

from .adb import AdbClient
from .collectors import default_collectors
from .models import RunStatus
from .runner import TestRunner
from .storage import Storage


def _device_worker(db_path: str, serial: str, duration: float, run_id: str,
                   heartbeat: mp.Queue, interval: float) -> None:
    stopped = threading.Event()

    def beat() -> None:
        while not stopped.wait(interval):
            heartbeat.put((run_id, time.monotonic()))

    thread = threading.Thread(target=beat, name="autoperf-heartbeat", daemon=True)
    heartbeat.put((run_id, time.monotonic()))
    thread.start()
    try:
        storage = Storage(db_path)
        storage.initialize()
        TestRunner(storage, AdbClient(), default_collectors()).run(serial, duration, run_id)
    finally:
        stopped.set()
        heartbeat.put((run_id, time.monotonic()))


@dataclass(frozen=True, slots=True)
class WorkerResult:
    run_id: str
    serial: str
    exit_code: int


class DeviceSupervisor:
    """Runs one spawn-safe process per device and monitors process heartbeats."""

    def __init__(self, storage: Storage, heartbeat_timeout: float = 10.0,
                 heartbeat_interval: float = 1.0):
        self.storage = storage
        self.heartbeat_timeout = heartbeat_timeout
        self.heartbeat_interval = heartbeat_interval

    def run_many(self, serials: list[str], duration: float) -> list[WorkerResult]:
        if not serials or len(serials) != len(set(serials)):
            raise ValueError("At least one unique device serial is required")
        context = mp.get_context("spawn")
        heartbeat = context.Queue()
        processes: dict[str, tuple[str, mp.Process]] = {}
        last_seen: dict[str, float] = {}
        for serial in serials:
            run_id = uuid.uuid4().hex
            self.storage.create_run(run_id, serial)
            process = context.Process(
                target=_device_worker,
                args=(self.storage.path, serial, duration, run_id, heartbeat, self.heartbeat_interval),
                name=f"autoperf-{serial}",
            )
            process.start()
            processes[run_id] = (serial, process)
            last_seen[run_id] = time.monotonic()

        alive = set(processes)
        while alive:
            try:
                run_id, timestamp = heartbeat.get(timeout=min(0.5, self.heartbeat_timeout))
                last_seen[run_id] = timestamp
            except queue.Empty:
                pass
            now = time.monotonic()
            for run_id in list(alive):
                _, process = processes[run_id]
                if not process.is_alive():
                    process.join()
                    alive.remove(run_id)
                    if process.exitcode and self.storage.get_run(run_id)["status"] not in ("failed", "interrupted"):
                        self.storage.update_run(run_id, RunStatus.FAILED, error=f"worker exited with code {process.exitcode}")
                elif now - last_seen[run_id] > self.heartbeat_timeout:
                    process.terminate()
                    process.join(timeout=5)
                    alive.remove(run_id)
                    self.storage.update_run(run_id, RunStatus.FAILED, error="worker heartbeat timed out")
        heartbeat.close()
        return [WorkerResult(run_id, serial, process.exitcode or 0)
                for run_id, (serial, process) in processes.items()]
