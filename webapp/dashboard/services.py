from __future__ import annotations

import uuid

from django.conf import settings

from autoperf.scenarios import youtube as youtube_scenarios
from autoperf.storage import Storage
from config.celery import app as celery_app

from .tasks import run_test_task


def get_storage() -> Storage:
    return Storage(settings.AUTOPERF_DB_PATH)


def get_queue_status(storage: Storage, timeout: float = 1.0) -> dict:
    """Reports Celery/Redis queue state, treating "no worker replied" as normal.

    inspect().active()/.reserved()/.scheduled() broadcast over the broker and
    return None if zero workers reply within `timeout` -- that's a completely
    ordinary state for a dashboard that isn't always running a worker, not an
    error. Only a real broker-connection failure (Redis itself unreachable)
    should read as broken, and it's reported as a *distinct* state so the UI
    can tell "nobody's listening" apart from "the broker itself is down" --
    two different problems with two different fixes.

    Separately, `running_runs` comes straight from Storage, not from Celery's
    inspect() at all -- `--pool=solo` (required on Windows, and required for
    TestRunner.run()'s SIGINT handling) is fully synchronous, so the worker
    cannot answer an inspect() broadcast while it's busy actually executing a
    task; inspect() will under-report (often to zero) for exactly the
    duration a run is in progress. Storage.list_running_runs() has no such
    blind spot -- TestRunner.run() updates the row's checkpoint continuously
    regardless of what Celery's control plane can see -- so it's the
    reliable source for "is something actually running right now."
    """
    try:
        inspector = celery_app.control.inspect(timeout=timeout)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}
    except Exception as exc:
        return {
            "broker_reachable": False,
            "worker_online": False,
            "workers": [],
            "running_runs": storage.list_running_runs(),
            "error": str(exc),
        }

    names = sorted(set(active) | set(reserved) | set(scheduled))
    workers = [
        {
            "name": name,
            "active": active.get(name, []),
            "reserved": reserved.get(name, []),
            "scheduled": scheduled.get(name, []),
        }
        for name in names
    ]
    return {
        "broker_reachable": True,
        "worker_online": bool(names),
        "workers": workers,
        "running_runs": storage.list_running_runs(),
    }


def trigger_run(storage: Storage, serial: str, duration: float, youtube_scenario: str | None = None) -> str:
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
    run_test_task.delay(storage.path, serial, duration, run_id, youtube_scenario)
    return run_id


def trigger_suite(storage: Storage, serial: str, tier: str, duration: float) -> list[str]:
    """Enqueues one run per scenario in `tier`, returning all their run_ids.

    Each scenario becomes its own independent trigger_run() call. Celery's
    `--pool=solo` worker only ever executes one task at a time anyway, so
    these naturally run one after another in enqueue order -- no separate
    sequencing/orchestration needed here.
    """
    return [
        trigger_run(storage, serial, duration, name)
        for name in youtube_scenarios.list_scenarios(tier=tier)
    ]
