from __future__ import annotations

import signal
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from .adapters import HOME, Adapter, ScenarioStep, VerificationError
from .adb import AdbClientProtocol
from .collectors import Collector
from .models import RunStatus, TestEvent
from .storage import BatchWriter, Storage


class DeviceBusyError(RuntimeError):
    pass


def _step_details(step: ScenarioStep) -> dict:
    """Scenario kwargs reduced to something JSON-serialisable.

    Step kwargs now carry Selector/Target objects, which the batch writer
    would choke on when it serialises an event's details.
    """
    safe: dict = {}
    for key, value in step.kwargs.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            safe[key] = value
        else:
            safe[key] = getattr(value, "name", None) or type(value).__name__
    return safe


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
    # The control tick below is deliberately short (10ms) to keep collector
    # timeout and scenario-step scheduling responsive. The checkpoint write,
    # however, must NOT run at that rate: update_run() opens its own SQLite
    # connection, sets PRAGMAs, UPDATEs and COMMITs, so ticking it would mean
    # ~100 write transactions/second all competing with BatchWriter for the
    # single WAL write lock. A 60s run survives that (~6k transactions); a
    # multi-hour soak run does not -- the writer falls behind, its bounded
    # queue fills, and put() raises "Metrics queue is full", killing the run
    # precisely because it ran long. Throttling to 1s keeps checkpoints fresh
    # enough to resume from while making heartbeat cost independent of run
    # length.
    heartbeat_interval: float = 1.0

    def _record_step_outcome(self, writer, run_id: str, step: ScenarioStep, future: Future) -> None:
        """Turn a finished scenario step into events.

        Three outcomes are kept distinct on purpose:

        - `verification_failed` -- the step could not be satisfied (element
          absent, wrong app in front, nothing playing). Previously impossible
          to express: `adb shell input tap` succeeds on empty space, so a
          missed tap was recorded as a completed action and the run finished
          green having measured nothing.
        - `selector_fallback` -- the step worked, but only by falling through
          to its hardcoded coordinate. That is exactly as reliable as the old
          behaviour, so it is not a failure; it is the early warning that a
          selector has decayed, and it is invisible unless recorded.
        - `adapter_error` -- everything else, unchanged.
        """
        details = {"action": step.action, **_step_details(step)}
        try:
            outcome = future.result()
        except VerificationError as exc:
            writer.put(TestEvent(run_id, "verification_failed", str(exc), details=details))
            return
        except Exception as exc:
            writer.put(TestEvent(run_id, "adapter_error", str(exc), details=details))
            return
        if isinstance(outcome, dict):
            details.update(outcome)
            if outcome.get("strategy") == "coordinates":
                writer.put(TestEvent(
                    run_id, "selector_fallback",
                    f"{outcome.get('target') or step.action} resolved by coordinates, not by selector",
                    details=details,
                ))
            if outcome.get("dumps"):
                # The instrumentation's own cost, recorded because it competes
                # with what is being measured: `uiautomator dump` takes seconds
                # on a real device and is CPU-heavy, and the collectors are
                # sampling that CPU at the same moment. Without this the
                # contamination is real and invisible -- CPU attributed to the
                # app that this tool spent looking at the screen.
                writer.put(TestEvent(
                    run_id, "ui_introspection",
                    f"{outcome['dumps']} UI dump(s) over "
                    f"{outcome.get('dump_seconds', 0)}s to resolve "
                    f"{outcome.get('target') or step.action}",
                    details=details,
                ))
        writer.put(TestEvent(run_id, "adapter_action", f"{step.action} completed", details=details))

    def _stop_driven_app(self, serial: str, writer, run_id: str) -> None:
        """Leave the device quiet, whether the run ended or was cancelled.

        A run leaves whatever app it was driving on screen -- typically a
        video still playing. Force-stopping it, rather than pressing Home, is
        what actually matters: YouTube and most media apps keep playing in the
        background once merely backgrounded, so Home alone stops nothing.

        The package comes from the scenario's own first step, which is always
        a `launch_app` carrying a `package` kwarg; Home is the fallback if
        that ever stops being true. Safe to call more than once -- force-stop
        is idempotent, which is what lets the cancel path call it early
        without complicating the normal end-of-run path.

        Best-effort throughout: a failure here must not stop the run's own
        status from being recorded.
        """
        if self.adapter is None:
            return
        package = self.scenario[0].kwargs.get("package") if self.scenario else None
        try:
            if package:
                self.adapter.stop_app(self.adb, serial, package)
            else:
                self.adapter.key_event(self.adb, serial, HOME)
        except Exception as exc:
            action = ({"action": "stop_app", "package": package} if package
                      else {"action": "key_event", "keycode": HOME})
            writer.put(TestEvent(run_id, "adapter_error", str(exc), details=action))

    def _record_app_version(self, serial: str, run_id: str) -> None:
        """Store which build of the app under test this run measured.

        Without it a background app update between a baseline and its
        candidate is indistinguishable from a device regression -- and it is
        the likelier of the two explanations.
        """
        package = None
        for step in self.scenario or []:
            if step.action == "launch_app" and step.kwargs.get("package"):
                package = step.kwargs["package"]
                break
        if not package:
            return
        from . import uiauto

        self.storage.set_run_app_version(run_id, uiauto.package_version(self.adb, serial, package))

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
        last_heartbeat = started
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
            # Read once at the start, before the scenario has had a chance to
            # change anything. Best-effort: an unreadable version is worth
            # far less than the run itself, so it must never abort one.
            try:
                self._record_app_version(serial, run_id)
            except Exception as exc:
                writer.put(TestEvent(run_id, "app_version_unavailable", str(exc)))
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
                        self._record_step_outcome(writer, run_id, step, future)
                    elif now - submitted >= self.adapter_action_timeout and idx not in scenario_timed_out:
                        scenario_timed_out.add(idx)
                        writer.put(TestEvent(run_id, "adapter_timeout",
                                             f"adapter action exceeded {self.adapter_action_timeout}s",
                                             details={"action": step.action, **_step_details(step)}))
                while next_step < len(scenario_steps) and now - started >= scenario_steps[next_step].at:
                    step = scenario_steps[next_step]
                    action = getattr(self.adapter, step.action)
                    scenario_active[next_step] = (executor.submit(action, self.adb, serial, **step.kwargs), now, step)
                    next_step += 1
                if now - last_heartbeat >= self.heartbeat_interval:
                    last_heartbeat = now
                    self.storage.update_run(run_id, RunStatus.RUNNING, checkpoint=str(now - started))
                # A short control tick keeps timeout and scenario-step scheduling
                # responsive; actual sampling frequency is still governed by
                # collector intervals, and the checkpoint write by heartbeat_interval.
                time.sleep(min(0.01, max(0.001, duration - (time.monotonic() - started))))
            # Captured before the adapter cleanup below, which can take seconds
            # (stop_app waits on the device) and would otherwise be counted as
            # run time. Written as the final checkpoint so `--resume` sees the
            # exact interruption point rather than whatever the last throttled
            # heartbeat happened to record -- including for a run cut short
            # before its first heartbeat ever fired.
            final_elapsed = time.monotonic() - started
            if stop:
                # Quiet the device *before* waiting on in-flight work, not
                # after. `cancel_futures` only drops futures that have not
                # started; a running one cannot be interrupted, and an
                # element-locating step can spend several seconds dumping the
                # UI and retrying. Measured on a Galaxy A55: a cancel landing
                # mid-`tap_element` left the video playing for 10.2 seconds
                # while shutdown() waited, which is what a user cancelling a
                # run actually complains about.
                #
                # When someone cancels, stopping the device outranks the
                # outcome of whatever action is still in flight. The regular
                # cleanup below still runs -- force-stop is idempotent, and
                # the second call also covers the normal end-of-run path.
                self._stop_driven_app(serial, writer, run_id)
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
                self._record_step_outcome(writer, run_id, step, future)
            status = RunStatus.INTERRUPTED if stop else RunStatus.COMPLETED
            self._stop_driven_app(serial, writer, run_id)
            if stop and self.adapter is not None:
                # A tap that was already in flight when the app was stopped
                # lands afterwards, on whatever is now on screen. Observed on
                # a Galaxy A55: a cancelled run finished with the Galaxy Store
                # open, because the last queued tap hit the launcher. Harmless
                # but untidy, and the next run should not start from another
                # app's screen.
                try:
                    self.adapter.key_event(self.adb, serial, HOME)
                except Exception:
                    pass
            writer.put(TestEvent(run_id, "lifecycle", f"run {status}"))
            writer.close()
            self.storage.update_run(run_id, status, checkpoint=str(final_elapsed))
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
