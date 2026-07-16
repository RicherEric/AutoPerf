from __future__ import annotations

import uuid

from django.conf import settings

from autoperf.storage import Storage

from .tasks import run_test_task


def get_storage() -> Storage:
    return Storage(settings.AUTOPERF_DB_PATH)


def trigger_run(storage: Storage, serial: str, duration: float) -> str:
    """Enqueue a test run on the Celery worker and return immediately.

    Task execution itself must still land on the main thread of a fresh
    process for TestRunner.run()'s SIGINT handling to work (signal.signal
    only works on the main thread of the main interpreter) -- see
    dashboard.tasks.run_test_task's docstring for how the Celery
    `--pool=solo` worker satisfies that. This function's own job is just
    durable enqueueing: create the run row synchronously so it's visible
    immediately, then hand off to Celery/Redis instead of spawning a raw
    OS process directly, so a run survives a Celery worker restart (Redis
    still holds the queued task) the same way it already survived a
    Django dev server restart.
    """
    run_id = uuid.uuid4().hex
    storage.create_run(run_id, serial)
    run_test_task.delay(storage.path, serial, duration, run_id)
    return run_id
