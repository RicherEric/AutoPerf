from __future__ import annotations

import uuid

from django.conf import settings

from autoperf.analyzer import compare, compute_stats
from autoperf.models import utc_now
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
    storage.create_run(run_id, serial, youtube_scenario)
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


def get_dashboard_stats(storage: Storage, recent_limit: int = 50) -> dict:
    """Aggregates recent completed runs into a pass/fail verdict, a per-scenario
    breakdown, and a metric trend, for the dashboard's stats/home page.

    A run's "verdict" is derived from the same baseline comparison analyzer.py
    already does for a single run (GET /api/runs/<id>/comparison) -- there's no
    separate pass/fail concept invented here, just this same check applied
    across recent history instead of one run at a time. A run whose device has
    no baseline set is its own "no_baseline" bucket (not counted as pass or
    fail) since there's nothing to compare it against yet.
    """
    runs = storage.list_runs(limit=recent_limit)
    completed = [r for r in runs if r["status"] == "completed"]

    baseline_cache: dict[str, dict | None] = {}
    verdicts = []
    trend_by_metric: dict[str, list[dict]] = {}

    for run in reversed(completed):  # chronological order for trend charts
        run_stats = compute_stats(storage.list_samples(run["id"], limit=100_000))
        for name, stat in run_stats.items():
            trend_by_metric.setdefault(name, []).append({"timestamp": run["started_at"], "value": stat.mean})

        device = run["device_serial"]
        if device not in baseline_cache:
            baseline_row = storage.get_baseline(device)
            baseline_cache[device] = (
                compute_stats(storage.list_samples(baseline_row["run_id"], limit=100_000))
                if baseline_row else None
            )
        baseline_stats = baseline_cache[device]

        if baseline_stats is None:
            verdict = "no_baseline"
        else:
            results = compare(baseline_stats, run_stats)
            verdict = "fail" if any(r.regressed for r in results) else "pass"

        verdicts.append({"run_id": run["id"], "scenario": run["youtube_scenario"], "verdict": verdict})

    passed = sum(1 for v in verdicts if v["verdict"] == "pass")
    failed = sum(1 for v in verdicts if v["verdict"] == "fail")
    no_baseline = sum(1 for v in verdicts if v["verdict"] == "no_baseline")
    evaluated = passed + failed

    by_scenario: dict[str, dict] = {}
    for v in verdicts:
        key = v["scenario"] or "(no scenario)"
        bucket = by_scenario.setdefault(key, {"pass": 0, "fail": 0, "no_baseline": 0})
        bucket[v["verdict"]] += 1
    scenario_stats = [
        {
            "scenario": name,
            **counts,
            "pass_rate": (counts["pass"] / (counts["pass"] + counts["fail"])) if (counts["pass"] + counts["fail"]) else None,
        }
        for name, counts in sorted(by_scenario.items())
    ]

    today = utc_now()[:10]
    runs_today = sum(1 for r in runs if (r["started_at"] or "")[:10] == today)

    return {
        "total_runs": len(runs),
        "runs_today": runs_today,
        "passed": passed,
        "failed": failed,
        "no_baseline": no_baseline,
        "pass_rate": (passed / evaluated) if evaluated else None,
        "by_scenario": scenario_stats,
        "trend": trend_by_metric,
    }
