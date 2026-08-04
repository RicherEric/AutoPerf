from __future__ import annotations

import json
import re
import statistics
import threading
import uuid

from django.conf import settings

from autoperf import campaigns
from autoperf.analyzer import compare, stats_from_aggregates
from autoperf.models import RunOrigin, RunStatus, utc_now
from autoperf.scenarios import youtube as youtube_scenarios
from autoperf.storage import Storage
from config.celery import app as celery_app

from .tasks import run_preflight_task, run_test_task

# The regression threshold analyzer.compare() is called with throughout the
# dashboard. Defined here at module top because several functions below take
# it as a default argument, which is evaluated at definition time. Sourced
# from the core so the CLI and the dashboard cannot drift into judging the
# same run by two different criteria.
DEFAULT_REGRESSION_THRESHOLD_PCT = campaigns.DEFAULT_REGRESSION_THRESHOLD_PCT

# What a run costs beyond its sampling `duration`: launching the app, waiting
# for it to reach the foreground, verifying, force-stopping it, plus a UI dump
# for the scenarios that locate elements by identity. Used to space a
# campaign's child tasks (see start_campaign). Being wrong here is not
# expensive in either direction -- too small and tasks queue behind a busy
# device exactly as before, too large and the device idles briefly between
# runs -- so it is deliberately a round number rather than a measurement
# pretending to be exact.
PER_RUN_OVERHEAD_SECONDS = 20


def get_storage() -> Storage:
    return Storage(settings.AUTOPERF_DB_PATH)


def recording_path(run_id: str):
    """Where livescreen/server.py's ffmpeg remux (see its _start_recording)
    writes a run's screen recording, if one was ever made -- a run only gets
    one if someone had the live screen panel open while it ran."""
    return settings.RECORDINGS_ROOT / f"{run_id}.mp4"


def get_recording_info(run_id: str) -> dict:
    path = recording_path(run_id)
    if not path.is_file():
        return {"exists": False, "url": None}
    return {"exists": True, "url": f"{settings.RECORDINGS_URL}{run_id}.mp4"}


def delete_recording(run_id: str) -> None:
    path = recording_path(run_id)
    path.unlink(missing_ok=True)


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
        # The one identity that does not change with how the device is
        # attached. An adb serial does: the same phone is `R5CXC006TZD` over
        # USB and `192.168.0.106:39235` over WiFi, so it registers twice and
        # reads as two devices -- which would have "both" run at once, against
        # one physical phone, each unaware of the other. `ro.serialno` is what
        # makes those two rows recognisable as one thing.
        hardware_serial = _getprop_value(props, "ro.serialno")

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
            "hardware_serial": hardware_serial,
            "chrome_version": chrome_version,
            "wifi_ip": wifi_ip,
            "user_agent": user_agent,
        }
        storage.register_device(
            device, android_version=android_version, battery_level=battery_level,
            connection=connection, device_name=device_name, extra_info=extra_info,
        )
    return storage.list_devices()


def dispatch_pending(storage: Storage, serial: str) -> str | None:
    """Hands one waiting run to Celery for this device, if it is free.

    Exactly one, and only when nothing of that device's is running. The
    dashboard used to enqueue a campaign's whole plan up front -- 500 tasks
    per device -- and that had three consequences that took a night to
    understand. Every worker that picked up a task for a busy device raised
    DeviceBusyError and retried it, so the workers spent their time refusing
    work while a second device never got started; a retried task went to the
    back, so a tier ran out of order; and because the queue and the rows were
    two copies of one intention, purging the queue left hundreds of rows
    pending with nothing able to run them.

    One task in flight per device removes all three: there is nothing to
    refuse, order is row order, and the queue holds so little that losing it
    costs one run rather than a campaign.
    """
    if storage.device_has_inflight_run(serial):
        return None
    run = storage.next_pending_run(serial)
    if run is None:
        return None
    # Marked before it is queued, not after: if the mark failed we would
    # rather skip a dispatch (the stall check picks it up again) than queue
    # the same run twice.
    storage.mark_run_queued(run["id"])
    run_test_task.apply_async(
        args=[storage.path, serial, run["duration"] or 60.0, run["id"],
              run["youtube_scenario"], json.loads(run["blind_targets"] or "null")],
        task_id=run["id"],
    )
    return run["id"]


def dispatch_stalled(storage: Storage) -> list[str]:
    """Restarts dispatch wherever it stopped. Cheap enough to poll.

    A chain only continues while something is alive to continue it: kill a
    worker mid-run and the next run is never handed out, and the campaign sits
    there looking queued forever. This asks the database the same question
    from outside -- which devices have work waiting and nothing running -- and
    is called from the endpoints the UI already polls, so the system repairs
    itself while somebody is looking at it.
    """
    return [run_id for serial in storage.devices_with_pending_runs()
            if (run_id := dispatch_pending(storage, serial))]


def get_running_runs_only(storage: Storage) -> dict:
    """The one part of the queue view that Celery is not needed for.

    `get_queue_status` costs three inspect() broadcasts, and a busy
    `--pool=solo` worker cannot answer any of them until it finishes its
    task: measured at 3.2 seconds per call while runs were in progress. A
    page that polls it every few seconds to answer "is anything running"
    therefore spends its whole life waiting for the answer it already had --
    Storage knows, in about a millisecond, and without the solo pool's blind
    spot. Mission Control asks this instead.
    """
    # Repairs dispatch wherever it stopped -- a killed worker breaks the
    # chain, and nothing else would ever restart it. Safe to call this often
    # because a run already handed to the queue is not handed out again until
    # REDISPATCH_AFTER_SECONDS has passed (Storage.next_pending_run).
    dispatch_stalled(storage)
    return {"running_runs": storage.list_running_runs()}


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
        # Counted from the database, not from Celery. A busy solo worker
        # answers no inspect() broadcast, so `reserved` reads as zero while
        # hundreds of tasks are in fact held -- a queue page that says
        # "nothing queued" next to a run list full of pending runs is the
        # page contradicting itself. This number cannot have that blind spot.
        "queued_runs": storage.count_queued_runs(),
    }


def trigger_run(storage: Storage, serial: str, duration: float, youtube_scenario: str | None = None,
                blind_targets: list[str] | None = None) -> str:
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
    storage.create_run(run_id, serial, youtube_scenario, origin=RunOrigin.DASHBOARD,
                       duration=duration, blind_targets=blind_targets)
    # Through the dispatcher, so a run asked for while the device is busy
    # waits its turn in the table rather than bouncing off DeviceBusyError.
    dispatch_pending(storage, serial)
    return run_id


def trigger_preflight(storage: Storage, serial: str, scenario: str | None = None) -> str:
    """Enqueue a selector check on the same worker that executes runs.

    Same queue rather than a side channel, because the constraint is physical:
    one device can only be driven by one thing at a time. Sharing the queue is
    what makes "wait your turn" the default instead of something every caller
    has to remember.
    """
    preflight_id = uuid.uuid4().hex
    storage.create_preflight(preflight_id, serial, scenario)
    run_preflight_task.apply_async(
        args=[storage.path, serial, preflight_id, scenario], task_id=preflight_id,
    )
    return preflight_id


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


def trigger_campaign(storage: Storage, kind: str, serial: str, duration: float, *,
                     scenario: str | None = None, tier: str | None = None,
                     iterations: int = 1) -> dict:
    """Create a campaign and enqueue every run it comprises.

    Planning, validation and the campaign row itself belong to
    `autoperf.campaigns`; this adds only the dashboard's dispatch strategy.
    All child runs are enqueued up front through the ordinary
    `trigger_run`/`run_test_task` path rather than being driven by a
    long-lived orchestrator task. The alternative -- one Celery task looping
    for the campaign's whole duration -- looked simpler but behaves badly on
    this stack: the Windows-compatible `--pool=solo` worker executes exactly
    one task at a time, so a multi-hour campaign would monopolise it and
    stall every other device's runs, and because Celery re-delivers an
    unacknowledged task, a worker restart mid-campaign would silently begin
    again from iteration one.

    Pre-enqueueing instead reuses machinery that already works: runs against
    the same device are serialised by Storage.try_start_run() (with
    run_test_task retrying on DeviceBusyError), and queued work survives a
    worker restart because Redis still holds it.

    Child runs are enqueued with a growing `countdown` rather than all at
    once, and that is not throttling -- it is what makes two devices actually
    run at the same time. One Celery queue is FIFO: a 500-run campaign
    submitted first puts 500 tasks ahead of the second device's first one, and
    every worker that picks one up finds that device busy, retries it, and
    picks up the next task for the same device. Measured on two phones: the
    second device had not started a single run after 90 seconds, with three
    workers idle-spinning on rejections. Spacing each run by roughly how long
    a run takes means a device's Nth task only becomes *due* around when that
    device is free, so the ready queue holds at most a few tasks per device
    and the workers spend their time running rather than rejecting.

    Consequently no component "owns" campaign progress here, so its status is
    derived from its child runs on read -- see campaigns.derived_status. The
    CLI, whose process *is* the campaign, uses campaigns.execute_campaign
    instead and shares everything else.
    """
    spec = campaigns.CampaignSpec(
        kind=kind, serial=serial, duration=duration,
        scenario=scenario, tier=tier, iterations=iterations,
    )
    created = campaigns.create_campaign(storage, spec)
    # The plan lives in the table; Celery is handed one run at a time.
    #
    # Enqueueing all of it up front was tried twice and failed differently
    # each time. Without spacing, one queue is FIFO: 500 tasks for the first
    # device sit ahead of the second device's first, every worker that takes
    # one finds that device busy and retries it to the back, so a tier ran
    # shuffled and the second device never started at all. With spacing, each
    # task's start time is fixed when the campaign is created, and reality
    # drifts away from it: runs slower than their slot pile up and collide,
    # runs faster leave the devices idle -- measured at 18 queued and nothing
    # running. Handing out one and chaining the next from the finishing task
    # has neither problem, because the schedule is whatever actually happens.
    dispatch_pending(storage, serial)
    storage.update_campaign(created["campaign_id"], RunStatus.RUNNING)
    return created


def cancel_campaign(storage: Storage, campaign_id: str) -> dict:
    return campaigns.cancel_campaign(storage, campaign_id)


def get_campaign_detail(storage: Storage, campaign_id: str,
                        threshold_pct: float = DEFAULT_REGRESSION_THRESHOLD_PCT) -> dict:
    return campaigns.campaign_detail(storage, campaign_id, threshold_pct)


def list_campaigns(storage: Storage, limit: int = 50, device_serial: str | None = None) -> list[dict]:
    return campaigns.campaign_summaries(storage, limit=limit, device_serial=device_serial)


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
    # `recent_limit` counts runs that have something to say, not rows. A
    # campaign pre-creates its whole plan, so 500 queued children per device
    # sit at the top of the table: taking the newest 50 rows returned 50
    # pending ones, every bucket empty, and a stats page that showed nothing
    # at all while the devices were visibly working.
    runs = storage.list_runs(limit=recent_limit, device_serial=device_serial,
                             include_queued=False)
    completed = [r for r in runs if r["status"] == "completed"]

    baseline_cache: dict[tuple[str, str], dict | None] = {}
    baseline_run_id_cache: dict[tuple[str, str], str | None] = {}
    verdicts = []
    trend_by_metric: dict[str, list[dict]] = {}

    # Chronological by when the run actually *ran*, not by row order. A
    # campaign pre-creates its children in one go, so their row order says
    # when they were planned, not when they executed -- plotted against it a
    # trend line doubles back on itself, which is how the chart looked.
    completed.sort(key=lambda run: run["started_at"] or "")

    for run in completed:
        run_stats = stats_from_aggregates(storage.aggregate_samples(run["id"]))
        # Only when the page is scoped to one phone. Two devices' means in one
        # line is not a trend: an A55 at 80% battery and a tablet at 100% draw
        # a line that jumps between them every point, and the same for memory,
        # where the two are simply different machines. The merged view still
        # answers pass-rate questions honestly; it just cannot answer this one.
        if device_serial:
            for name, stat in run_stats.items():
                trend_by_metric.setdefault(name, []).append({
                    "timestamp": run["started_at"],
                    "value": stat.mean,
                    "scenario": run["youtube_scenario"],
                })

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
                stats_from_aggregates(storage.aggregate_samples(baseline_row["run_id"]))
                if baseline_row else None
            )
            baseline_run_id_cache[cache_key] = baseline_row["run_id"] if baseline_row else None
        baseline_stats = baseline_cache[cache_key]

        regressed_metrics = []
        quality = storage.run_quality(run["id"])
        if not quality["verified"]:
            # Checked before the baseline comparison, and given its own
            # bucket rather than being called a fail: the scenario never
            # reached the screen it was supposed to measure, so these numbers
            # do not describe the thing being judged at all. Calling it a
            # pass would hide a broken test; calling it a fail would report a
            # performance problem that was never observed. Same reasoning as
            # the existing no_baseline bucket.
            verdict = "unverified"
        elif baseline_stats is None:
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
            "quality": quality,
            "app_version": {
                "package": run.get("app_package"),
                "version_name": run.get("app_version_name"),
                "version_code": run.get("app_version_code"),
            },
        })

    passed = sum(1 for v in verdicts if v["verdict"] == "pass")
    failed = sum(1 for v in verdicts if v["verdict"] == "fail")
    no_baseline = sum(1 for v in verdicts if v["verdict"] == "no_baseline")
    unverified = sum(1 for v in verdicts if v["verdict"] == "unverified")
    # Deliberately excluded from the denominator, like no_baseline: a pass
    # rate computed over runs that never reached their target screen would
    # be a confident number about nothing.
    evaluated = passed + failed

    by_scenario: dict[str, dict] = {}
    for v in verdicts:
        key = v["scenario"] or "(no scenario)"
        bucket = by_scenario.setdefault(
            key, {"pass": 0, "fail": 0, "no_baseline": 0, "unverified": 0}
        )
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
        "unverified": unverified,
        "pass_rate": (passed / evaluated) if evaluated else None,
        "threshold_pct": threshold_pct,
        "by_scenario": scenario_stats,
        "trend": trend_by_metric,
        # Why the trend is empty, when it is -- so the page can say so instead
        # of rendering an empty box that reads as "no data".
        "trend_scope": "device" if device_serial else "all_devices",
        # What triggered the runs behind these numbers. A pass rate averaged
        # over a scheduled campaign and a handful someone kicked off while
        # watching the phone is two different claims added together, and until
        # `origin` existed there was no way to see that had happened -- a CLI
        # run and a dashboard run wrote identical rows. Counted over all runs
        # for this device, not just the recent window, so the filter a UI
        # builds from it offers every value that actually exists.
        "by_origin": storage.run_origin_counts(device_serial),
        "runs": list(reversed(verdicts)),  # most-recent-first for a "recent verdicts" table
    }
