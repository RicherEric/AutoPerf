from __future__ import annotations

import signal
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from .adapters import HOME, Adapter, ScenarioStep
from .adb import AdbClientProtocol
from .collectors import Collector
from .models import RunStatus, TestEvent
from .storage import BatchWriter, Storage


class DeviceBusyError(RuntimeError):
    pass


@dataclass(slots=True)
class TestRunner:
    storage: Storage
    adb: AdbClientProtocol
    collectors: list[Collector]
    collector_timeout: float = 15.0
    max_workers: int | None = None
    adapter: Adapter | None = None
    scenario: list[ScenarioStep] | None = None
    adapter_action_timeout: float = 10.0
    cancel_check_interval: float = 1.0

    def run(self, serial: str, duration: float, run_id: str | None = None) -> str:
        if self.scenario and self.adapter is None:
            raise ValueError("scenario requires an adapter")
        for step in self.scenario or []:
            if not callable(getattr(self.adapter, step.action, None)):
                raise ValueError(f"Adapter has no action '{step.action}'")
        run_id = run_id or uuid.uuid4().hex
        existing = self.storage.get_run(run_id)
        if existing is None:
            self.storage.create_run(run_id, serial)
        elif existing["device_serial"] != serial:
            raise ValueError("Run belongs to another device")
        else:
            # A queued (Celery-pending) run can be cancelled before its task
            # ever executes -- but on a --pool=solo worker that's busy with a
            # prior task, the revoke control message can't be processed until
            # the worker frees up, by which point the task may already have
            # been dequeued and started (the same control-plane blind spot
            # documented in dashboard.services.get_queue_status). Storage's
            # cancel_requested flag is checked here, before anything starts,
            # so a run already marked cancelled is never resurrected back to
            # running/completed regardless of what Celery's revoke() managed.
            if existing.get("cancel_requested"):
                self.storage.update_run(run_id, RunStatus.INTERRUPTED, error="cancelled before starting")
                return run_id
        if not self.storage.try_start_run(run_id):
            raise DeviceBusyError(f"device {serial} already has a run in progress")
        writer = BatchWriter(self.storage)
        writer.start()
        stop = False

        def request_stop(*_: object) -> None:
            nonlocal stop
            stop = True

        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, request_stop)
        started = time.monotonic()
        last_cancel_check = started
        due = {collector.name: started for collector in self.collectors}
        active: dict[str, tuple[Future, float, Collector]] = {}
        timed_out: set[str] = set()
        scenario_steps = sorted(self.scenario or [], key=lambda step: step.at)
        scenario_active: dict[int, tuple[Future, float, ScenarioStep]] = {}
        scenario_timed_out: set[int] = set()
        next_step = 0
        executor = ThreadPoolExecutor(
            max_workers=self.max_workers or max(1, len(self.collectors) + (1 if scenario_steps else 0)),
            thread_name_prefix="autoperf-collector",
        )
        try:
            writer.put(TestEvent(run_id, "lifecycle", "run started"))
            while not stop and time.monotonic() - started < duration:
                now = time.monotonic()
                # Dashboard-triggered runs execute in a separate Celery worker
                # process, so SIGINT (from a local Ctrl+C) can't reach them --
                # this is the remote-cancel equivalent, polled at ~1s rather
                # than every tick to avoid hammering the DB.
                if now - last_cancel_check >= self.cancel_check_interval:
                    last_cancel_check = now
                    current = self.storage.get_run(run_id)
                    if current and current.get("cancel_requested"):
                        stop = True
                        break
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
                for idx, (future, submitted, step) in list(scenario_active.items()):
                    if future.done():
                        del scenario_active[idx]
                        if idx in scenario_timed_out:
                            scenario_timed_out.remove(idx)
                            continue
                        try:
                            future.result()
                            writer.put(TestEvent(run_id, "adapter_action", f"{step.action} completed",
                                                 details={"action": step.action, **step.kwargs}))
                        except Exception as exc:
                            writer.put(TestEvent(run_id, "adapter_error", str(exc),
                                                 details={"action": step.action, **step.kwargs}))
                    elif now - submitted >= self.adapter_action_timeout and idx not in scenario_timed_out:
                        scenario_timed_out.add(idx)
                        writer.put(TestEvent(run_id, "adapter_timeout",
                                             f"adapter action exceeded {self.adapter_action_timeout}s",
                                             details={"action": step.action, **step.kwargs}))
                while next_step < len(scenario_steps) and now - started >= scenario_steps[next_step].at:
                    step = scenario_steps[next_step]
                    action = getattr(self.adapter, step.action)
                    scenario_active[next_step] = (executor.submit(action, self.adb, serial, **step.kwargs), now, step)
                    next_step += 1
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
            for idx, (future, _, step) in scenario_active.items():
                if idx in scenario_timed_out or future.cancelled():
                    continue
                try:
                    future.result()
                    writer.put(TestEvent(run_id, "adapter_action", f"{step.action} completed",
                                         details={"action": step.action, **step.kwargs}))
                except Exception as exc:
                    writer.put(TestEvent(run_id, "adapter_error", str(exc),
                                         details={"action": step.action, **step.kwargs}))
            status = RunStatus.INTERRUPTED if stop else RunStatus.COMPLETED
            if self.adapter is not None:
                # Whether a scenario run ends normally or gets cancelled, it
                # leaves whatever app was mid-action on screen (e.g. a video
                # still playing) -- return to the home screen so the device
                # is in a clean state for whoever runs the next test. Best-
                # effort: a failure here shouldn't prevent the run's own
                # status from being recorded.
                try:
                    self.adapter.key_event(self.adb, serial, HOME)
                except Exception as exc:
                    writer.put(TestEvent(run_id, "adapter_error", str(exc), details={"action": "key_event", "keycode": HOME}))
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
