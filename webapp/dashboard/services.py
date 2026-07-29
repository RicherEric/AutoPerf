from __future__ import annotations

import re
import statistics
import threading
import uuid

from django.conf import settings

from autoperf.analyzer import compare, compute_trend, stats_from_aggregates
from autoperf.models import RunStatus, utc_now
from autoperf.scenarios import youtube as youtube_scenarios
from autoperf.storage import Storage
from config.celery import app as celery_app

from .tasks import run_test_task

# The regression threshold analyzer.compare() is called with throughout the
# dashboard. Defined here at module top because several functions below take
# it as a default argument, which is evaluated at definition time.
DEFAULT_REGRESSION_THRESHOLD_PCT = 20.0


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


CAMPAIGN_KINDS = ("soak", "repeat")

# A ceiling on how many child runs one campaign may enqueue at once. Not a
# resource limit so much as a typo guard: "repeat the regression tier 10000
# times" is far more likely to be a slipped digit than an intent, and it
# would otherwise sit in Redis for weeks.
MAX_CAMPAIGN_RUNS = 500


def plan_campaign_runs(kind: str, scenario: str | None, tier: str | None,
                       iterations: int) -> list[str | None]:
    """The ordered list of scenarios to run, one entry per child run.

    Tier campaigns are ordered iteration-major (a full sweep of the tier,
    then the next full sweep) rather than scenario-major (all N repeats of
    scenario A, then all N of scenario B). It matters when a campaign is cut
    short: stopping halfway through an iteration-major campaign leaves an
    even number of samples for every scenario, whereas scenario-major would
    leave the first few scenarios fully sampled and the rest with none --
    the flaky-rate comparison across scenarios would be worthless.

    `None` entries mean a plain sampling run with no scenario driving the
    device, which is a legitimate soak subject.
    """
    if kind == "soak":
        return [scenario]
    if tier:
        names = youtube_scenarios.list_scenarios(tier=tier)
        return [name for _ in range(iterations) for name in names]
    return [scenario] * iterations


def trigger_campaign(storage: Storage, kind: str, serial: str, duration: float, *,
                     scenario: str | None = None, tier: str | None = None,
                     iterations: int = 1) -> dict:
    """Create a campaign and enqueue every run it comprises.

    All child runs are enqueued up front through the ordinary
    `trigger_run()`/`run_test_task` path rather than being driven by a
    long-lived orchestrator task. The alternative -- one Celery task that
    loops for the campaign's whole duration -- looked simpler but behaves
    badly on this stack: the Windows-compatible `--pool=solo` worker executes
    exactly one task at a time, so a multi-hour campaign would monopolise it
    and stall every other device's runs, and because Celery re-delivers an
    unacknowledged task, a worker restart mid-campaign would silently begin
    again from iteration one.

    Pre-enqueueing instead reuses machinery that already works: runs against
    the same device are serialised by Storage.try_start_run() (with
    run_test_task retrying on DeviceBusyError), queued work survives a worker
    restart because Redis still holds it, and a campaign competes fairly with
    other devices' runs instead of blocking them.

    Consequently no component "owns" campaign progress, so its status is
    derived from its child runs on read -- see get_campaign_detail.
    """
    if kind not in CAMPAIGN_KINDS:
        raise ValueError(f"kind must be one of {CAMPAIGN_KINDS}")
    if not serial:
        raise ValueError("serial is required")
    if duration <= 0:
        raise ValueError("duration must be positive")
    if tier and tier not in youtube_scenarios.TIERS:
        raise ValueError(f"tier must be one of {youtube_scenarios.TIERS}")
    if kind == "repeat":
        if not scenario and not tier:
            raise ValueError("a repeat campaign needs either a scenario or a tier")
        if iterations < 1:
            raise ValueError("iterations must be at least 1")
    else:
        # A soak is one continuous run by definition; accepting an iteration
        # count here would quietly mean something different from what the
        # caller asked for.
        iterations = 1

    planned = plan_campaign_runs(kind, scenario, tier, iterations)
    if len(planned) > MAX_CAMPAIGN_RUNS:
        raise ValueError(
            f"campaign would enqueue {len(planned)} runs, above the limit of {MAX_CAMPAIGN_RUNS}"
        )

    campaign_id = uuid.uuid4().hex
    storage.create_campaign(campaign_id, kind, serial, duration,
                            scenario=scenario, tier=tier, iterations=iterations)
    run_ids = []
    for name in planned:
        run_id = uuid.uuid4().hex
        storage.create_run(run_id, serial, name, campaign_id=campaign_id)
        run_test_task.apply_async(args=[storage.path, serial, duration, run_id, name], task_id=run_id)
        run_ids.append(run_id)
    storage.update_campaign(campaign_id, RunStatus.RUNNING)
    return {"campaign_id": campaign_id, "run_ids": run_ids, "count": len(run_ids)}


def cancel_campaign(storage: Storage, campaign_id: str) -> dict:
    """Cancel a campaign and everything it has queued or running."""
    campaign = storage.get_campaign(campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    storage.request_campaign_cancel(campaign_id)
    flagged = storage.cancel_campaign_runs(campaign_id)
    storage.update_campaign(campaign_id, RunStatus.INTERRUPTED)
    return {"campaign_id": campaign_id, "cancelled_runs": flagged, "status": "cancelling"}


TERMINAL_RUN_STATUSES = ("completed", "failed", "interrupted")

# Buckets used when reducing a soak run for trend fitting. Enough resolution
# to see the shape of a multi-hour run without refitting over 100k raw rows.
SOAK_TREND_BUCKETS = 200


def _derived_campaign_status(campaign: dict, runs: list[dict]) -> str:
    """A campaign's status, computed from its child runs.

    Nothing orchestrates a campaign while it executes (see
    trigger_campaign's docstring), so there is no process in a position to
    write "completed" at the right moment. Deriving it on read is what keeps
    the status honest across a worker restart, a cancelled campaign, and runs
    that a busy device is still retrying.
    """
    if campaign.get("cancel_requested"):
        return "interrupted"
    if not runs:
        return campaign["status"]
    if all(run["status"] in TERMINAL_RUN_STATUSES for run in runs):
        return "completed"
    return "running"


def _run_metric_means(storage: Storage, run_id: str) -> dict[str, float]:
    return {name: stat.mean for name, stat in
            stats_from_aggregates(storage.aggregate_samples(run_id)).items()}


def _repeat_analysis(storage: Storage, runs: list[dict],
                     threshold_pct: float) -> dict:
    """Per-scenario stability across repeated identical runs.

    Two failure modes are counted separately rather than merged into one
    pass rate, because they call for different responses: a run that never
    reached 'completed' is a broken test or a device problem, while a
    completed run whose metrics regressed past the threshold is a
    performance finding. Merging them would hide which one is happening.

    A scenario is reported `flaky` when the same scenario, run repeatedly
    against the same device with the same duration, produced both passes and
    failures -- with every input held constant, a mixed outcome is the
    definition of flakiness, and it is exactly what a single run can never
    reveal no matter how carefully it is inspected.
    """
    by_scenario: dict[str, list[dict]] = {}
    for run in runs:
        by_scenario.setdefault(run["youtube_scenario"] or "", []).append(run)

    baseline_cache: dict[tuple[str, str], dict | None] = {}
    scenarios = []
    for scenario, scenario_runs in sorted(by_scenario.items()):
        errored = [r for r in scenario_runs if r["status"] in ("failed", "interrupted")]
        completed = [r for r in scenario_runs if r["status"] == "completed"]
        pending = [r for r in scenario_runs if r["status"] not in TERMINAL_RUN_STATUSES]

        regressed, metric_means = [], {}
        for run in completed:
            means = _run_metric_means(storage, run["id"])
            for name, value in means.items():
                metric_means.setdefault(name, []).append(value)

            cache_key = (run["device_serial"], scenario)
            if cache_key not in baseline_cache:
                baseline_row = storage.get_baseline(run["device_serial"], scenario)
                baseline_cache[cache_key] = (
                    stats_from_aggregates(storage.aggregate_samples(baseline_row["run_id"]))
                    if baseline_row else None
                )
            baseline_stats = baseline_cache[cache_key]
            if baseline_stats is not None:
                results = compare(
                    baseline_stats,
                    stats_from_aggregates(storage.aggregate_samples(run["id"])),
                    threshold_pct=threshold_pct,
                )
                if any(r.regressed for r in results):
                    regressed.append(run["id"])

        # Spread of each metric's per-run mean across iterations. A metric
        # whose mean swings widely between identical runs makes any
        # single-run baseline comparison unreliable, so this is reported
        # alongside the pass counts rather than buried.
        stability = []
        for name, values in sorted(metric_means.items()):
            mean_of_means = statistics.fmean(values)
            spread = statistics.pstdev(values) if len(values) > 1 else 0.0
            stability.append({
                "name": name,
                "runs": len(values),
                "mean": mean_of_means,
                "stdev": spread,
                # Coefficient of variation: spread expressed relative to the
                # metric's own level, so cpu % and memory KiB are comparable.
                "cv_pct": (spread / mean_of_means * 100) if mean_of_means else None,
                "minimum": min(values),
                "maximum": max(values),
            })

        finished = len(completed) + len(errored)
        passed = len(completed) - len(regressed)
        scenarios.append({
            "scenario": scenario,
            "total": len(scenario_runs),
            "pending": len(pending),
            "completed": len(completed),
            "errored": len(errored),
            "regressed": len(regressed),
            "regressed_run_ids": regressed,
            "pass_rate": (passed / finished * 100) if finished else None,
            "flaky": finished > 1 and 0 < passed < finished,
            "metric_stability": stability,
        })
    return {"scenarios": scenarios}


def _soak_analysis(storage: Storage, runs: list[dict]) -> dict:
    """Drift/leak verdict for a soak campaign's long run.

    Reads the run through downsample_samples rather than raw rows -- a soak
    run is precisely the case where loading every sample is unaffordable,
    and compute_trend wants evenly spaced points anyway.
    """
    candidates = [r for r in runs if r["status"] in ("completed", "running")] or runs
    if not candidates:
        return {"run_id": None, "trends": []}
    run = candidates[0]
    trends = compute_trend(storage.downsample_samples(run["id"], buckets=SOAK_TREND_BUCKETS))
    return {
        "run_id": run["id"],
        "status": run["status"],
        "trends": [
            {
                "name": trend.name,
                "slope_per_hour": trend.slope_per_hour,
                "start_mean": trend.start_mean,
                "end_mean": trend.end_mean,
                "drift_pct": trend.drift_pct,
                "span_hours": trend.span_hours,
                "points": trend.points,
            }
            for trend in sorted(trends.values(), key=lambda t: t.name)
        ],
    }


def get_campaign_detail(storage: Storage, campaign_id: str,
                        threshold_pct: float = DEFAULT_REGRESSION_THRESHOLD_PCT) -> dict:
    campaign = storage.get_campaign(campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    runs = storage.list_campaign_runs(campaign_id)
    finished = sum(1 for run in runs if run["status"] in TERMINAL_RUN_STATUSES)
    detail = {
        **campaign,
        "status": _derived_campaign_status(campaign, runs),
        "stored_status": campaign["status"],
        "run_count": len(runs),
        "finished_count": finished,
        "progress_pct": (finished / len(runs) * 100) if runs else 0.0,
        "threshold_pct": threshold_pct,
        "runs": runs,
    }
    if campaign["kind"] == "soak":
        detail["soak"] = _soak_analysis(storage, runs)
    else:
        detail["repeat"] = _repeat_analysis(storage, runs, threshold_pct)
    return detail


def list_campaigns(storage: Storage, limit: int = 50, device_serial: str | None = None) -> list[dict]:
    campaigns = storage.list_campaigns(limit=limit, device_serial=device_serial)
    for campaign in campaigns:
        total = campaign["run_count"] or 0
        finished = (campaign["completed_count"] or 0) + (campaign["failed_count"] or 0)
        campaign["stored_status"] = campaign["status"]
        # Same derivation as get_campaign_detail, but from the counts the
        # list query already computed rather than re-reading every run row.
        if campaign.get("cancel_requested"):
            campaign["status"] = "interrupted"
        elif total and finished == total:
            campaign["status"] = "completed"
        elif total:
            campaign["status"] = "running"
        campaign["finished_count"] = finished
        campaign["progress_pct"] = (finished / total * 100) if total else 0.0
    return campaigns


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
    runs = storage.list_runs(limit=recent_limit, device_serial=device_serial)
    completed = [r for r in runs if r["status"] == "completed"]

    baseline_cache: dict[tuple[str, str], dict | None] = {}
    baseline_run_id_cache: dict[tuple[str, str], str | None] = {}
    verdicts = []
    trend_by_metric: dict[str, list[dict]] = {}

    for run in reversed(completed):  # chronological order for trend charts
        run_stats = stats_from_aggregates(storage.aggregate_samples(run["id"]))
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
                stats_from_aggregates(storage.aggregate_samples(baseline_row["run_id"]))
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
