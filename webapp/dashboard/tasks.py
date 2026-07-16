from __future__ import annotations

from celery import shared_task

from autoperf.adapters import AndroidAdapter
from autoperf.adb import AdbClient
from autoperf.collectors import default_collectors
from autoperf.runner import TestRunner
from autoperf.scenarios import youtube as youtube_scenarios
from autoperf.storage import Storage


@shared_task(name="dashboard.run_test", ignore_result=True)
def run_test_task(db_path: str, serial: str, duration: float, run_id: str, youtube_scenario: str | None = None) -> None:
    """Runs a test as a Celery task instead of a raw multiprocessing.Process.

    Celery's Windows-compatible `--pool=solo` executes each task directly in
    the worker's own main process, on its main thread -- the same property
    the old multiprocessing.Process spawn relied on for TestRunner.run()'s
    SIGINT handling (signal.signal only works on the main thread of the main
    interpreter). Swapping the execution substrate keeps that constraint
    satisfied while adding a durable, retryable queue in front of it.
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
    TestRunner(storage, adb, default_collectors(), adapter=adapter, scenario=scenario).run(serial, duration, run_id)
