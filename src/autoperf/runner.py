from __future__ import annotations

import signal
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from .adb import AdbClientProtocol
from .collectors import Collector
from .models import RunStatus, TestEvent
from .storage import BatchWriter, Storage


@dataclass(slots=True)
class TestRunner:
    storage: Storage
    adb: AdbClientProtocol
    collectors: list[Collector]
    collector_timeout: float = 15.0
    max_workers: int | None = None

    def run(self, serial: str, duration: float, run_id: str | None = None) -> str:
        run_id = run_id or uuid.uuid4().hex
        existing = self.storage.get_run(run_id)
        if existing is None:
            self.storage.create_run(run_id, serial)
        elif existing["device_serial"] != serial:
            raise ValueError("Run belongs to another device")
        self.storage.update_run(run_id, RunStatus.RUNNING)
        writer = BatchWriter(self.storage)
        writer.start()
        stop = False

        def request_stop(*_: object) -> None:
            nonlocal stop
            stop = True

        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, request_stop)
        started = time.monotonic()
        due = {collector.name: started for collector in self.collectors}
        active: dict[str, tuple[Future, float, Collector]] = {}
        timed_out: set[str] = set()
        executor = ThreadPoolExecutor(
            max_workers=self.max_workers or max(1, len(self.collectors)),
            thread_name_prefix="autoperf-collector",
        )
        try:
            writer.put(TestEvent(run_id, "lifecycle", "run started"))
            while not stop and time.monotonic() - started < duration:
                now = time.monotonic()
                for name, (future, submitted, collector) in list(active.items()):
                    if future.done():
                        del active[name]
                        if name in timed_out:
                            timed_out.remove(name)
                            continue
                        try:
                            for sample in future.result():
                                writer.put(sample)
                        except Exception as exc:
                            writer.put(TestEvent(run_id, "collector_error", str(exc), details={"collector": name}))
                    elif now - submitted >= self.collector_timeout and name not in timed_out:
                        timed_out.add(name)
                        writer.put(TestEvent(run_id, "collector_timeout",
                                             f"collector exceeded {self.collector_timeout}s",
                                             details={"collector": name}))
                for collector in self.collectors:
                    if now >= due[collector.name] and collector.name not in active:
                        active[collector.name] = (
                            executor.submit(collector.collect, self.adb, serial, run_id), now, collector
                        )
                        due[collector.name] = now + collector.interval
                self.storage.update_run(run_id, RunStatus.RUNNING, checkpoint=str(time.monotonic() - started))
                # A short control tick keeps timeout and heartbeat state responsive;
                # actual sampling frequency is still governed by collector intervals.
                time.sleep(min(0.01, max(0.001, duration - (time.monotonic() - started))))
            # Finish in-flight ADB calls before closing the writer so their final
            # samples cannot be lost at the duration boundary.
            executor.shutdown(wait=True, cancel_futures=True)
            for name, (future, _, _) in active.items():
                if name in timed_out or future.cancelled():
                    continue
                try:
                    for sample in future.result():
                        writer.put(sample)
                except Exception as exc:
                    writer.put(TestEvent(run_id, "collector_error", str(exc), details={"collector": name}))
            status = RunStatus.INTERRUPTED if stop else RunStatus.COMPLETED
            writer.put(TestEvent(run_id, "lifecycle", f"run {status}"))
            writer.close()
            self.storage.update_run(run_id, status)
        except Exception as exc:
            try:
                writer.close()
            finally:
                self.storage.update_run(run_id, RunStatus.FAILED, error=str(exc))
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            signal.signal(signal.SIGINT, previous)
        return run_id
