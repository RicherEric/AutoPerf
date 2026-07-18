from __future__ import annotations

from celery import shared_task

from autoperf.adapters import AndroidAdapter
from autoperf.adb import AdbClient
from autoperf.collectors import default_collectors
from autoperf.runner import DeviceBusyError, TestRunner
from autoperf.scenarios import youtube as youtube_scenarios
from autoperf.storage import Storage

# How long a task waits before re-checking a device that's already running
# another test -- see the DeviceBusyError handling below.
DEVICE_BUSY_RETRY_COUNTDOWN = 2


@shared_task(bind=True, name="dashboard.run_test", ignore_result=True, max_retries=None)
def run_test_task(self, db_path: str, serial: str, duration: float, run_id: str, youtube_scenario: str | None = None) -> None:
    """Runs a test as a Celery task instead of a raw multiprocessing.Process.

    Celery's Windows-compatible `--pool=solo` executes each task directly in
    the worker's own main process, on its main thread -- the same property
    the old multiprocessing.Process spawn relied on for TestRunner.run()'s
    SIGINT handling (signal.signal only works on the main thread of the main
    interpreter). Swapping the execution substrate keeps that constraint
    satisfied while adding a durable, retryable queue in front of it.

    With more than one worker/concurrency slot (see scripts/start-worker.py),
    two tasks could otherwise be dispatched for the same device at once.
    TestRunner.run() refuses that via Storage.try_start_run() and raises
    DeviceBusyError, which this task turns into an indefinite retry
    (max_retries=None -- the same run just waits its turn, the same way it
    always would have behind the old single-worker queue) rather than racing
    two adb sessions against the same device.
    """
    storage = Storage(db_path)
    storage.initialize()
    adb = AdbClient()
    adapter = None
    scenario = None
    if youtube_scenario:
        adapter = AndroidAdapter()
        screen = adapter.screen_size(adb, serial)
        scenario = youtube_scenarios.build(youtube_scenario, screen)
    try:
        TestRunner(storage, adb, default_collectors(), adapter=adapter, scenario=scenario).run(serial, duration, run_id)
    except DeviceBusyError as exc:
        raise self.retry(exc=exc, countdown=DEVICE_BUSY_RETRY_COUNTDOWN)
