from __future__ import annotations

from celery import shared_task

from autoperf import demo, preflight, uiauto
from autoperf.models import RunOrigin, RunStatus
from autoperf.profiles import select_profile
from autoperf.adb import AdbClient
from autoperf.runner import DeviceBusyError, TestRunner
from autoperf.scenarios import youtube as youtube_scenarios
from autoperf.storage import Storage

# How long a task waits before re-checking a device that's already running
# another test -- see the DeviceBusyError handling below.
DEVICE_BUSY_RETRY_COUNTDOWN = 2


@shared_task(bind=True, name="dashboard.run_test", ignore_result=True, max_retries=None)
def run_test_task(self, db_path: str, serial: str, duration: float, run_id: str,
                  youtube_scenario: str | None = None,
                  blind_targets: list[str] | None = None) -> None:
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
    if storage.get_run(run_id) is None:
        storage.create_run(run_id, serial, youtube_scenario, origin=RunOrigin.DASHBOARD)
    adb = AdbClient()
    # One probe: the adapter that drives this device and the metrics worth
    # sampling on it are the same platform's answer.
    profile = select_profile(adb, serial)
    adapter = None
    scenario = None
    if youtube_scenario:
        adapter = profile.adapter()
        screen = adapter.screen_size(adb, serial)
        scenario = youtube_scenarios.build(youtube_scenario, screen)
        # Opt-in only, and only ever set by an explicit request: strips the
        # named targets' selectors so they fall through to their coordinate,
        # reproducing a decayed selector table on demand. See autoperf.demo.
        scenario = demo.blind_targets(scenario, blind_targets)
    try:
        TestRunner(storage, adb, profile.collectors(), adapter=adapter, scenario=scenario).run(serial, duration, run_id)
        completed = storage.get_run(run_id)
        if (
            completed
            and completed["status"] == "completed"
            and storage.get_baseline(serial, youtube_scenario) is None
        ):
            storage.set_baseline(serial, run_id)
    except DeviceBusyError as exc:
        raise self.retry(exc=exc, countdown=DEVICE_BUSY_RETRY_COUNTDOWN)


@shared_task(bind=True, name="dashboard.run_preflight", ignore_result=True, max_retries=None)
def run_preflight_task(self, db_path: str, serial: str, preflight_id: str,
                       scenario: str | None = None) -> None:
    """Grade the selector table against one device. Measures nothing.

    Queued through the same worker as a run, and claiming the same device, for
    one reason: preflight drives the phone. Its taps are real and its dumps are
    the most expensive thing this project ever asks a device to do. Letting it
    overlap a measured run would put the tool's own cost inside the numbers the
    run reports -- the failure this codebase spends most of its design avoiding.

    `scenario` scopes the check to the targets one preset actually uses.
    Without it the whole covering set runs, which is 11 scenarios and 42 target
    checks -- right for "is the table still good", far too slow to sit in front
    of anything.
    """
    storage = Storage(db_path)
    storage.initialize()
    if storage.get_preflight(preflight_id) is None:
        storage.create_preflight(preflight_id, serial, scenario)
    if not storage.try_start_preflight(preflight_id):
        raise self.retry(exc=DeviceBusyError(f"device {serial} is busy"),
                         countdown=DEVICE_BUSY_RETRY_COUNTDOWN)

    adb = AdbClient()
    try:
        profile = select_profile(adb, serial)
        adapter = profile.adapter()
        names = [scenario] if scenario else preflight.covering_scenarios()
        summary = preflight.run_preflight(adb, adapter, serial,
                                          scenarios=names, profile=profile)
        version = None
        for package in preflight.app_packages(names):
            version = uiauto.package_version(adb, serial, package)
            if version:
                break
        storage.finish_preflight(preflight_id, RunStatus.COMPLETED,
                                 summary=summary, app_version=version)
    except Exception as exc:
        # A preflight that dies has to say so. Left 'running' it would hold the
        # device lock against every later run on this serial.
        storage.finish_preflight(preflight_id, RunStatus.FAILED, error=str(exc))
        raise
