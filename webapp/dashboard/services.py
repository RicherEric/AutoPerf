from __future__ import annotations

import re
import threading
import uuid

from django.conf import settings

from autoperf.analyzer import compare, compute_stats
from autoperf.models import RunStatus, utc_now
from autoperf.scenarios import youtube as youtube_scenarios
from autoperf.storage import Storage
from config.celery import app as celery_app

from .tasks import run_test_task


def get_storage() -> Storage:
    return Storage(settings.AUTOPERF_DB_PATH)


# adb-over-WiFi devices show up in `adb devices -l` with the connect address
# itself (host:port) as their serial, unlike USB devices' hardware serials --
# this is a free way to label connection type with no extra adb round-trip.
_WIRELESS_SERIAL_RE = re.compile(r"^[A-Za-z0-9.\-]+:\d+$")


def _shell_or_none(adb, serial: str, command: str) -> str | None:
    try:
        return adb.shell(serial, command).strip() or None
    except Exception:
        return None


def _getprop_value(props_blob: str, key: str) -> str | None:
    match = re.search(rf"\[{re.escape(key)}\]: \[(.*?)\]", props_blob)
    return (match.group(1) or None) if match else None


def _chrome_version(props_output: str | None) -> str | None:
    if not props_output:
        return None
    match = re.search(r"versionName=(\S+)", props_output)
    return match.group(1) if match else None


def _wifi_ip(ip_output: str | None) -> str | None:
    if not ip_output:
        return None
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ip_output)
    return match.group(1) if match else None


def _build_user_agent(android_version, model, build_id, chrome_version) -> str | None:
    # Mirrors the real WebView/Chrome UA format -- omitted entirely (rather
    # than guessing) when the pieces needed to build it honestly aren't
    # available, since this is meant to be real device info, not a plausible
    # fabrication.
    if not (android_version and model and build_id):
        return None
    chrome_part = f"Chrome/{chrome_version} " if chrome_version else ""
    return (
        f"Mozilla/5.0 (Linux; Android {android_version}; {model} Build/{build_id}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) {chrome_part}Mobile Safari/537.36"
    )


def refresh_devices(storage: Storage, adb) -> list[dict]:
    """Re-scans connected/paired devices and enriches each with identity info
    useful for telling phones apart in a classroom demo with many devices
    connected at once: Android version, battery %, USB vs WiFi, manufacturer/
    brand, SDK level, build ID, CPU ABI, WiFi IP, installed Chrome version,
    and a constructed User-Agent string built only from values that were
    actually read back from the device.

    Each enrichment call is best-effort and independent -- a device that's
    unauthorized, mid-reconnect, or otherwise unresponsive to one `adb shell`
    call still gets registered with whatever info *did* come back, rather
    than the whole refresh failing. Per-device property reads are batched
    into one `getprop` call rather than one shell round-trip per property,
    since a classroom refresh may cover many devices at once.
    """
    for device in adb.devices():
        props = _shell_or_none(adb, device.serial, "getprop") or ""
        android_version = _getprop_value(props, "ro.build.version.release")
        manufacturer = _getprop_value(props, "ro.product.manufacturer")
        brand = _getprop_value(props, "ro.product.brand")
        sdk_version = _getprop_value(props, "ro.build.version.sdk")
        build_id = _getprop_value(props, "ro.build.display.id")
        cpu_abi = _getprop_value(props, "ro.product.cpu.abi")

        battery_level = None
        battery_output = _shell_or_none(adb, device.serial, "dumpsys battery")
        if battery_output:
            match = re.search(r"level:\s*(\d+)", battery_output)
            battery_level = float(match.group(1)) if match else None

        # The phone's own user-set name (Settings > About phone > Device
        # name) -- an unset device returns the literal string "null".
        raw_name = _shell_or_none(adb, device.serial, "settings get global device_name")
        device_name = raw_name if raw_name and raw_name != "null" else None

        chrome_version = _chrome_version(_shell_or_none(adb, device.serial, "dumpsys package com.android.chrome"))
        wifi_ip = _wifi_ip(_shell_or_none(adb, device.serial, "ip -f inet addr show wlan0"))
        user_agent = _build_user_agent(android_version, device.model, build_id, chrome_version)

        connection = "wifi" if _WIRELESS_SERIAL_RE.match(device.serial) else "usb"
        extra_info = {
            "manufacturer": manufacturer,
            "brand": brand,
            "sdk_version": sdk_version,
            "build_id": build_id,
            "cpu_abi": cpu_abi,
            "chrome_version": chrome_version,
            "wifi_ip": wifi_ip,
            "user_agent": user_agent,
        }
        storage.register_device(
            device, android_version=android_version, battery_level=battery_level,
            connection=connection, device_name=device_name, extra_info=extra_info,
        )
    return storage.list_devices()


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
    inspect() at all -- a `--pool=solo` worker (required on Windows for
    TestRunner.run()'s SIGINT handling; see scripts/start-worker.py for how
    parallelism is achieved instead via multiple such worker processes) is
    fully synchronous, so a worker mid-task can't answer an inspect()
    broadcast for itself; inspect() will under-report exactly the tasks
    currently in progress on whichever workers are busy. Storage.list_running_runs()
    has no such blind spot -- TestRunner.run() updates the row's checkpoint
    continuously regardless of what Celery's control plane can see -- so
    it's the reliable source for "is something actually running right now."
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

    The Celery task_id is pinned to our own run_id (rather than letting
    Celery generate its own) so a not-yet-started run can be cancelled with
    `celery_app.control.revoke(run_id)` without needing a separate id
    mapping -- see cancel_run below.
    """
    run_id = uuid.uuid4().hex
    storage.create_run(run_id, serial, youtube_scenario)
    run_test_task.apply_async(args=[storage.path, serial, duration, run_id, youtube_scenario], task_id=run_id)
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


def _revoke_in_background(run_id: str) -> None:
    # Fire-and-forget: empirically, celery_app.control.revoke() can block for
    # a long time (observed: a Django request hanging well past 10s) when
    # the --pool=solo worker is busy synchronously executing a prior task --
    # the same solo-pool blind spot documented in get_queue_status, just
    # manifesting as a hang here instead of a silent miss. It's no longer
    # needed for correctness (TestRunner.run() itself refuses to resurrect an
    # already-cancelled run -- see runner.py), so it must never be allowed to
    # block the cancel request's HTTP response, no matter how long the
    # underlying broker round-trip actually takes.
    try:
        celery_app.control.revoke(run_id)
    except Exception:
        pass


def cancel_run(storage: Storage, run_id: str) -> dict:
    """Cancels a run regardless of whether it's still queued or already running.

    Storage.request_cancel() (setting cancel_requested=1) is the mechanism
    actually relied on for correctness in both cases, not Celery's control
    plane -- a "pending" task may already have been dequeued and started by
    the time any revoke reaches the worker (see _revoke_in_background's
    docstring), so the DB is updated to "interrupted" immediately and
    unconditionally rather than waiting on revoke() to confirm anything.
    TestRunner.run() itself checks cancel_requested before doing any work and
    refuses to resurrect an already-cancelled run back to running/completed
    even if Celery ran it anyway -- see runner.py. A "running" task can't be
    stopped via revoke(terminate=True) at all here (killing the worker
    process mid-task would also kill the SIGINT-handling main thread
    TestRunner.run() depends on) -- it winds down cooperatively once its
    loop notices the flag, the same way a local Ctrl+C already does.
    """
    run = storage.get_run(run_id)
    if run is None:
        raise ValueError("run not found")
    if run["status"] == "pending":
        storage.request_cancel(run_id)
        storage.update_run(run_id, RunStatus.INTERRUPTED, error="cancelled before starting")
        threading.Thread(target=_revoke_in_background, args=(run_id,), daemon=True).start()
        return {"run_id": run_id, "status": "interrupted"}
    if run["status"] == "running":
        storage.request_cancel(run_id)
        return {"run_id": run_id, "status": "cancelling"}
    raise ValueError(f"run is already {run['status']} -- nothing to cancel")


DEFAULT_REGRESSION_THRESHOLD_PCT = 20.0


def get_dashboard_stats(
    storage: Storage, recent_limit: int = 50, threshold_pct: float = DEFAULT_REGRESSION_THRESHOLD_PCT,
    device_serial: str | None = None,
) -> dict:
    """Aggregates recent completed runs into a pass/fail verdict, a per-scenario
    breakdown, a metric trend, and a per-run detail list explaining *why* each
    verdict landed where it did, for the dashboard's stats/home page.

    A run's "verdict" is derived from the same baseline comparison analyzer.py
    already does for a single run (GET /api/runs/<id>/comparison) -- there's no
    separate pass/fail concept invented here, just this same check applied
    across recent history instead of one run at a time. A run whose device has
    no baseline set is its own "no_baseline" bucket (not counted as pass or
    fail) since there's nothing to compare it against yet. `threshold_pct` is
    the same regression threshold analyzer.compare() uses, surfaced in the
    response so the UI can state the actual pass/fail criteria rather than
    leaving it implicit.

    `device_serial`, when given, scopes everything (the runs list, the
    trend, the per-scenario breakdown) to just that one phone -- useful in a
    classroom demo with many devices' runs otherwise all mixed together in
    one feed. Filtering happens at the storage query level (not by slicing
    an already-limited all-devices list) so a busy device's own history
    isn't crowded out of `recent_limit` by everyone else's runs.
    """
    runs = storage.list_runs(limit=recent_limit, device_serial=device_serial)
    completed = [r for r in runs if r["status"] == "completed"]

    baseline_cache: dict[tuple[str, str], dict | None] = {}
    baseline_run_id_cache: dict[tuple[str, str], str | None] = {}
    verdicts = []
    trend_by_metric: dict[str, list[dict]] = {}

    for run in reversed(completed):  # chronological order for trend charts
        run_stats = compute_stats(storage.list_samples(run["id"], limit=100_000))
        for name, stat in run_stats.items():
            trend_by_metric.setdefault(name, []).append({"timestamp": run["started_at"], "value": stat.mean})

        device = run["device_serial"]
        scenario = run["youtube_scenario"] or ""
        # Keyed by (device, scenario) -- not just device -- since a heavier
        # scenario naturally uses more CPU/memory than a lighter one with no
        # real regression involved; comparing across scenarios produced
        # meaningless deltas (e.g. "+778%" for a scenario that simply does
        # more on-screen work than whatever the baseline scenario did).
        cache_key = (device, scenario)
        if cache_key not in baseline_cache:
            baseline_row = storage.get_baseline(device, scenario)
            baseline_cache[cache_key] = (
                compute_stats(storage.list_samples(baseline_row["run_id"], limit=100_000))
                if baseline_row else None
            )
            baseline_run_id_cache[cache_key] = baseline_row["run_id"] if baseline_row else None
        baseline_stats = baseline_cache[cache_key]

        regressed_metrics = []
        if baseline_stats is None:
            verdict = "no_baseline"
        else:
            results = compare(baseline_stats, run_stats, threshold_pct=threshold_pct)
            regressed_metrics = [
                {"name": r.name, "delta_pct": r.delta_pct} for r in results if r.regressed
            ]
            verdict = "fail" if regressed_metrics else "pass"

        verdicts.append({
            "run_id": run["id"],
            "device_serial": device,
            "scenario": run["youtube_scenario"],
            "verdict": verdict,
            "started_at": run["started_at"],
            "baseline_run_id": baseline_run_id_cache.get(cache_key),
            "regressed_metrics": regressed_metrics,
        })

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
        "threshold_pct": threshold_pct,
        "by_scenario": scenario_stats,
        "trend": trend_by_metric,
        "runs": list(reversed(verdicts)),  # most-recent-first for a "recent verdicts" table
    }
