from __future__ import annotations

import json
from dataclasses import asdict

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from autoperf import demo, preflight
from autoperf.adb import AdbClient, AdbError
from autoperf.adapters import (
    BACK, HOME, DPAD_CENTER, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT, DPAD_UP,
)
from autoperf.analyzer import app_version_delta, compare, stats_from_aggregates
from autoperf.scenarios import youtube as youtube_scenarios

from .services import (
    cancel_campaign,
    cancel_run,
    delete_recording,
    get_campaign_detail,
    get_dashboard_stats,
    get_queue_status,
    get_running_runs_only,
    get_recording_info,
    get_storage,
    list_campaigns,
    refresh_devices,
    trigger_campaign,
    trigger_preflight,
    trigger_run,
    trigger_suite,
)


@require_http_methods(["GET"])
def demo_plan(request):
    """The demo's own definition, served so the page and the CLI agree.

    Both `scripts/demo.py` and the Demo page drive the same three minutes.
    Reading the plan from one place is what stops the browser showing a talk
    track the terminal is not following -- see autoperf.demo.
    """
    return JsonResponse({
        "prewarm": list(demo.DEMO_PREWARM),
        "integration": list(demo.DEMO_INTEGRATION),
        "integration_seconds": demo.DEMO_INTEGRATION_SECONDS,
    })


@require_http_methods(["GET"])
def devices(request):
    """Every device this database remembers; `?connected=1` for the ones here now.

    The stored list is deliberately not the attached list -- history belongs to
    a device whether or not it is plugged in today, and a run detail page has
    to be able to name the phone it ran on months later. But anything about to
    *drive* a device needs the other question answered, and it can only be
    answered by asking adb: `last_seen` is a timestamp, not a heartbeat, and a
    device that left is indistinguishable from one that is simply idle.

    Without this, "start on every device" would queue a night of work onto
    phones that went home a week ago.
    """
    rows = get_storage().list_devices()
    if request.GET.get("connected"):
        try:
            attached = {d.serial for d in AdbClient().devices() if d.state == "device"}
        except AdbError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        rows = _one_row_per_physical_device(
            [row for row in rows if row["serial"] in attached])
    return JsonResponse(rows, safe=False)


def _one_row_per_physical_device(rows: list[dict]) -> list[dict]:
    """Collapses the same phone reached two ways into one, USB winning.

    A device attached over USB *and* over adb-over-WiFi is two adb serials and
    therefore two rows, but one phone -- and `Storage.try_start_run` excludes
    by serial, so the lock that stops two runs sharing a device does not fire
    between them. "Start on every device" would put two runs on one phone,
    each measuring a screen the other was also driving.

    USB wins because it is the connection that does not depend on the network
    being willing, which is what a demo runs on.
    """
    best: dict[str, dict] = {}
    for row in rows:
        key = row.get("hardware_serial") or row["serial"]
        current = best.get(key)
        if current is None or (row.get("connection") == "usb"
                               and current.get("connection") != "usb"):
            best[key] = row
    return list(best.values())


@csrf_exempt
@require_http_methods(["POST"])
def devices_refresh(request):
    return JsonResponse(refresh_devices(get_storage(), AdbClient()), safe=False)


@csrf_exempt
@require_http_methods(["POST"])
def device_control(request, serial):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    action = body.get("action")
    keycodes = {
        "home": HOME, "back": BACK, "up": DPAD_UP, "down": DPAD_DOWN,
        "left": DPAD_LEFT, "right": DPAD_RIGHT, "enter": DPAD_CENTER,
    }
    adb = AdbClient()
    try:
        if action in keycodes:
            adb.shell(serial, f"input keyevent {keycodes[action]}")
        elif action == "tap":
            x, y = int(body.get("x")), int(body.get("y"))
            if x < 0 or y < 0:
                raise ValueError("tap coordinates must be non-negative")
            adb.shell(serial, f"input tap {x} {y}")
        else:
            return JsonResponse({"error": "unsupported control action"}, status=400)
    except (TypeError, ValueError, AdbError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "action": action})


@csrf_exempt
@require_http_methods(["POST"])
def devices_connect(request):
    """Connects to a device over adb-over-WiFi (classroom demo: students join
    via WiFi instead of USB) and returns the refreshed device list."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    address = body.get("address")
    if not address:
        return JsonResponse({"error": "address is required, e.g. 192.168.1.50:5555"}, status=400)

    try:
        message = AdbClient().connect(address)
    except (ValueError, AdbError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({"message": message, "devices": refresh_devices(get_storage(), AdbClient())})


@require_http_methods(["GET"])
def devices_mdns(request):
    """Discovers Android wireless-debugging services advertised over mDNS."""
    try:
        return JsonResponse(AdbClient().mdns_services())
    except AdbError as exc:
        return JsonResponse({"error": str(exc)}, status=503)


@csrf_exempt
@require_http_methods(["POST"])
def devices_connect_discovered(request):
    """Discovers and connects every already-paired ADB-over-WiFi service.

    Pairing services are deliberately ignored because Android requires the
    user-visible six-digit code before they can be trusted.
    """
    adb = AdbClient()
    try:
        discovery = adb.mdns_services()
    except AdbError as exc:
        return JsonResponse({"error": str(exc)}, status=503)

    results = []
    seen = set()
    for service in discovery["services"]:
        address = service["address"]
        if service["kind"] != "connect" or address in seen:
            continue
        seen.add(address)
        try:
            message = adb.connect(address)
            results.append({"address": address, "ok": True, "message": message})
        except (ValueError, AdbError) as exc:
            results.append({"address": address, "ok": False, "error": str(exc)})

    devices = refresh_devices(get_storage(), adb)
    return JsonResponse({
        "services": discovery["services"],
        "results": results,
        "devices": devices,
    })


@csrf_exempt
@require_http_methods(["POST"])
def devices_pair(request):
    """One-time adb-over-WiFi pairing step (Android 11+ "Wireless debugging
    -> Pair device with pairing code"), so a student's phone never needs a
    USB cable at all -- see AdbClient.pair()'s docstring for why this is a
    separate step (and a separate port) from devices_connect above."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    address = body.get("address")
    code = body.get("code")
    if not address or not code:
        return JsonResponse({"error": "address and code are required"}, status=400)

    try:
        message = AdbClient().pair(address, code)
    except (ValueError, AdbError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({"message": message})


@csrf_exempt
@require_http_methods(["POST"])
def device_nickname(request, serial):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    nickname = (body.get("nickname") or "").strip()
    storage = get_storage()
    storage.set_device_nickname(serial, nickname)
    return JsonResponse({"serial": serial, "nickname": nickname})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def runs(request):
    if request.method == "GET":
        storage = get_storage()
        # `?origin=manual` and `?device=<serial>` narrow the list; the shape
        # stays a plain array either way, and omitting both still means every
        # run. `device` is spelled the same here as on /api/stats,
        # /api/campaigns and /api/preflights -- one name for one concept, so a
        # caller filtering two of them does not have to remember which is
        # which. The counts that let a UI build these filters live on
        # /api/stats, where the other aggregates already are -- changing this
        # endpoint's shape to carry them would break every existing caller for
        # the sake of a number none of them asked for.
        # A campaign's queued children are hidden unless asked for: 500 rows
        # per device that have not happened yet would otherwise be the entire
        # visible history, and the runs that did happen would be off the end
        # of the limit. `?queued=1` brings them back.
        rows = storage.list_runs(
            device_serial=request.GET.get("device") or None,
            origin=request.GET.get("origin") or None,
            include_queued=bool(request.GET.get("queued")),
        )
        # Every row carries its own verdict. Without this the list cannot tell
        # a clean run from one that measured a screen it never reached -- they
        # both read "completed", which is the exact failure mode the
        # verification layer exists to make visible.
        quality = storage.run_quality_many([row["id"] for row in rows])
        for row in rows:
            row["quality"] = quality[row["id"]]
        return JsonResponse(rows, safe=False)

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    serial = body.get("serial")
    if not serial:
        return JsonResponse({"error": "serial is required"}, status=400)
    duration = float(body.get("duration", 60))

    youtube_scenario = body.get("youtube_scenario") or None
    if youtube_scenario and youtube_scenario not in youtube_scenarios.REGISTRY:
        return JsonResponse({"error": f"unknown youtube_scenario: {youtube_scenario!r}"}, status=400)

    blind = body.get("blind_targets") or None
    if blind is not None:
        if not isinstance(blind, list) or not all(isinstance(name, str) for name in blind):
            return JsonResponse({"error": "blind_targets must be a list of target names"}, status=400)
        unknown = sorted(set(blind) - set(demo.known_target_names()))
        if unknown:
            return JsonResponse({"error": f"unknown target(s): {', '.join(unknown)}"}, status=400)
        if not youtube_scenario:
            return JsonResponse({"error": "blind_targets needs a youtube_scenario to apply to"}, status=400)

    run_id = trigger_run(get_storage(), serial, duration, youtube_scenario, blind_targets=blind)
    return JsonResponse({"run_id": run_id, "status": "pending", "blind_targets": blind or []}, status=202)


@csrf_exempt
@require_http_methods(["GET", "DELETE"])
def run_detail(request, run_id):
    storage = get_storage()
    run = storage.get_run(run_id)
    if run is None:
        return JsonResponse({"error": "not found"}, status=404)

    if request.method == "DELETE":
        # Only a *running* run is refused, because deleting one would leave a
        # BatchWriter writing samples for a row that no longer exists. A
        # pending run holds nothing and touches no device -- and refusing it
        # made a queued campaign undeletable from the UI, which is precisely
        # when someone wants to clear it. Its Celery task, when it fires,
        # finds no row and stops (TestRunner's require_existing).
        if run["status"] == "running":
            return JsonResponse({"error": "run is still running -- cancel it first"}, status=400)
        # A baseline run is deletable, and deleting it clears the baseline
        # (Storage.delete_run cascades). Refusing instead was a deadlock: a
        # completed run auto-becomes its device+scenario's baseline when that
        # pair has none, so the first run of every pair was undeletable and
        # the advice the error gave -- set a different baseline first -- had
        # no second run to point at.
        storage.delete_run(run_id)
        delete_recording(run_id)
        return JsonResponse({"deleted": run_id})

    run["quality"] = storage.run_quality(run_id)
    return JsonResponse(run)


# What a run recorded about itself. Kept separate from the run detail because
# the log can be long and the header is polled while a run is live.
EVENT_LIMIT = 500


@require_http_methods(["GET"])
def run_events(request, run_id):
    storage = get_storage()
    if storage.get_run(run_id) is None:
        return JsonResponse({"error": "not found"}, status=404)

    requested = request.GET.get("kinds", "")
    kinds = tuple(k for k in (part.strip() for part in requested.split(",")) if k)
    limit = max(1, min(int(request.GET.get("limit", EVENT_LIMIT)), 2000))
    events = storage.list_run_events(run_id, kinds=kinds, limit=limit)
    return JsonResponse({
        "events": events,
        "quality": storage.run_quality(run_id),
        # So the client can say "showing 500 of N" rather than silently
        # truncating -- a cut-off log that looks complete is the same class of
        # lie this whole layer exists to prevent.
        "truncated": len(events) == limit,
    })


@require_http_methods(["GET"])
def run_recording(request, run_id):
    if get_storage().get_run(run_id) is None:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse(get_recording_info(run_id))


@csrf_exempt
@require_http_methods(["POST"])
def run_cancel(request, run_id):
    try:
        result = cancel_run(get_storage(), run_id)
    except ValueError as exc:
        message = str(exc)
        status = 404 if message == "run not found" else 400
        return JsonResponse({"error": message}, status=status)
    return JsonResponse(result)


@require_http_methods(["GET"])
def run_samples(request, run_id):
    since_id = int(request.GET.get("since_id", 0))
    limit = int(request.GET.get("limit", 1000))
    samples = get_storage().list_samples(run_id, since_id=since_id, limit=limit)
    for sample in samples:
        sample["labels"] = json.loads(sample["labels"])
    next_since_id = samples[-1]["id"] if samples else since_id
    return JsonResponse({"samples": samples, "next_since_id": next_since_id})


@require_http_methods(["GET"])
def run_series(request, run_id):
    """Chart-ready, bucket-averaged series for one run.

    `/samples` streams raw rows and is what the live-polling path uses, since
    during a run the client only ever asks for the handful of samples newer
    than `since_id`. Replaying a *finished* multi-hour run is the opposite
    shape: ~100k rows at once, which is both a large response and more points
    than an SVG line chart can draw. This returns a fixed point budget
    regardless of how long the run was, so an 8-hour soak costs the same to
    render as a 60-second smoke run.
    """
    buckets = max(1, min(int(request.GET.get("buckets", 300)), 2000))
    series = get_storage().downsample_samples(run_id, buckets=buckets)
    return JsonResponse({"series": series, "buckets": buckets})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def baseline(request, serial):
    storage = get_storage()
    if request.method == "GET":
        # Baselines are scenario-scoped (see Storage.get_baseline's
        # docstring) -- omit ?scenario= for the plain/no-scenario baseline.
        result = storage.get_baseline(serial, request.GET.get("scenario"))
        if result is None:
            return JsonResponse({"error": "no baseline set"}, status=404)
        return JsonResponse(result)

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    run_id = body.get("run_id")
    if not run_id:
        return JsonResponse({"error": "run_id is required"}, status=400)
    run = storage.get_run(run_id)
    if run is None:
        return JsonResponse({"error": "run not found"}, status=404)
    if run["device_serial"] != serial:
        return JsonResponse({"error": f"run belongs to device {run['device_serial']}, not {serial}"}, status=400)

    storage.set_baseline(serial, run_id)
    return JsonResponse(storage.get_baseline(serial, run["youtube_scenario"]))


@require_http_methods(["GET"])
def youtube_scenarios_list(request):
    tier = request.GET.get("tier") or None
    if tier and tier not in youtube_scenarios.TIERS:
        return JsonResponse({"error": f"unknown tier: {tier!r}"}, status=400)
    return JsonResponse(youtube_scenarios.describe_scenarios(tier=tier), safe=False)


@require_http_methods(["GET"])
def selector_targets(request):
    """Every UI target, so a client can offer one to blind. See autoperf.demo.

    With `?scenario=`, the targets that one preset actually resolves. An empty
    list is a real answer, not an error: the `play_*` deep-link presets resolve
    none, which is exactly why they cost zero UI dumps.
    """
    scenario = request.GET.get("scenario") or None
    if scenario is None:
        return JsonResponse({"targets": demo.known_target_names()})
    if scenario not in youtube_scenarios.REGISTRY:
        return JsonResponse({"error": f"unknown youtube_scenario: {scenario!r}"}, status=400)
    return JsonResponse({
        "scenario": scenario,
        "targets": sorted(preflight.targets_of(scenario)),
    })


# A preflight is a question about the selector table, not a measurement, so it
# gets its own resource rather than a flag on a run: it produces no samples,
# has no duration and can never be a baseline.
PREFLIGHT_LIST_LIMIT = 20


@csrf_exempt
@require_http_methods(["GET", "POST"])
def preflights(request):
    storage = get_storage()
    if request.method == "GET":
        limit = max(1, min(int(request.GET.get("limit", PREFLIGHT_LIST_LIMIT)), 100))
        return JsonResponse(
            storage.list_preflights(limit=limit, device_serial=request.GET.get("device") or None),
            safe=False,
        )

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    serial = body.get("serial")
    if not serial:
        return JsonResponse({"error": "serial is required"}, status=400)

    scenario = body.get("youtube_scenario") or None
    if scenario is not None:
        if scenario not in youtube_scenarios.REGISTRY:
            return JsonResponse({"error": f"unknown youtube_scenario: {scenario!r}"}, status=400)
        if not preflight.targets_of(scenario):
            # Refused rather than run. A preflight of a deep-link preset would
            # come back green having graded nothing, and a green report that
            # checked nothing is the exact shape of failure this tool exists
            # to find.
            return JsonResponse({
                "error": f"{scenario} resolves no selectors, so there is nothing to preflight",
                "targets": [],
            }, status=400)

    scenarios = [scenario] if scenario else preflight.covering_scenarios()
    preflight_id = trigger_preflight(storage, serial, scenario)
    return JsonResponse({
        "preflight_id": preflight_id,
        "status": "pending",
        "youtube_scenario": scenario,
        # So the caller can say how long this will take before it starts.
        "scenarios": scenarios,
        "targets": sorted({t for name in scenarios for t in preflight.targets_of(name)}),
    }, status=202)


@csrf_exempt
@require_http_methods(["POST"])
def preflight_cancel(request, preflight_id):
    """Asks a preflight to stop. It stops at the next scenario boundary.

    Not instant, and deliberately so -- see preflight.run_preflight's
    `should_stop`. What is instant is that it will not start another scenario,
    so the device comes back within one scenario's worth of taps instead of
    after the whole covering set.
    """
    storage = get_storage()
    if storage.get_preflight(preflight_id) is None:
        return JsonResponse({"error": "not found"}, status=404)
    if not storage.request_preflight_cancel(preflight_id):
        return JsonResponse({"error": "this preflight has already finished"}, status=400)
    return JsonResponse({"preflight_id": preflight_id, "cancel_requested": True})


@require_http_methods(["GET"])
def preflight_detail(request, preflight_id):
    report = get_storage().get_preflight(preflight_id)
    if report is None:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse(report)


@csrf_exempt
@require_http_methods(["POST"])
def suites(request):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    serial = body.get("serial")
    if not serial:
        return JsonResponse({"error": "serial is required"}, status=400)
    tier = body.get("tier")
    if tier not in youtube_scenarios.TIERS:
        return JsonResponse({"error": f"tier must be one of {youtube_scenarios.TIERS}"}, status=400)
    duration = float(body.get("duration", 30))

    run_ids = trigger_suite(get_storage(), serial, tier, duration)
    return JsonResponse({"tier": tier, "run_ids": run_ids, "count": len(run_ids)}, status=202)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def campaigns(request):
    storage = get_storage()
    if request.method == "GET":
        return JsonResponse(
            list_campaigns(storage, limit=int(request.GET.get("limit", 50)),
                           device_serial=request.GET.get("device") or None),
            safe=False,
        )
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    try:
        result = trigger_campaign(
            storage,
            kind=body.get("kind", ""),
            serial=body.get("serial", ""),
            duration=float(body.get("duration", 0)),
            scenario=body.get("scenario") or None,
            tier=body.get("tier") or None,
            iterations=int(body.get("iterations", 1)),
        )
    except (ValueError, TypeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(result, status=202)


@csrf_exempt
@require_http_methods(["GET", "DELETE"])
def campaign_detail(request, campaign_id):
    """GET returns the campaign with its analysis; DELETE removes it and
    everything it produced -- matching how /api/runs/<id> is shaped."""
    storage = get_storage()
    if request.method == "DELETE":
        if storage.get_campaign(campaign_id) is None:
            return JsonResponse({"error": "campaign not found"}, status=404)
        # Recordings live on disk outside the database, so they are cleaned
        # up here from the run ids delete_campaign reports, mirroring run
        # deletion.
        for run_id in storage.delete_campaign(campaign_id):
            delete_recording(run_id)
        return JsonResponse({"campaign_id": campaign_id, "deleted": True})
    try:
        return JsonResponse(get_campaign_detail(storage, campaign_id))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=404)


@csrf_exempt
@require_http_methods(["POST"])
def campaign_cancel(request, campaign_id):
    try:
        return JsonResponse(cancel_campaign(get_storage(), campaign_id))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=404)


@require_http_methods(["GET"])
def queue_status(request):
    # `?running=1` answers only "what is on a device right now", from the
    # database, without the three Celery broadcasts a busy solo worker cannot
    # reply to. Mission Control polls that; the Task Queue page, which is
    # about Celery itself, still asks the full question.
    if request.GET.get("running"):
        return JsonResponse(get_running_runs_only(get_storage()))
    return JsonResponse(get_queue_status(get_storage()))


@require_http_methods(["GET"])
def stats(request):
    recent_limit = int(request.GET.get("limit", 50))
    device_serial = request.GET.get("device") or None
    return JsonResponse(get_dashboard_stats(get_storage(), recent_limit=recent_limit, device_serial=device_serial))


@require_http_methods(["GET"])
def run_comparison(request, run_id):
    storage = get_storage()
    run = storage.get_run(run_id)
    if run is None:
        return JsonResponse({"error": "not found"}, status=404)
    baseline_row = storage.get_baseline(run["device_serial"], run["youtube_scenario"])
    if baseline_row is None:
        scenario_desc = f"scenario {run['youtube_scenario']!r}" if run["youtube_scenario"] else "plain runs"
        return JsonResponse(
            {"error": f"no baseline set for device {run['device_serial']} ({scenario_desc})"}, status=404
        )

    threshold_pct = float(request.GET.get("threshold", 20.0))
    baseline_stats = stats_from_aggregates(storage.aggregate_samples(baseline_row["run_id"]))
    candidate_stats = stats_from_aggregates(storage.aggregate_samples(run_id))
    results = compare(baseline_stats, candidate_stats, threshold_pct=threshold_pct)
    return JsonResponse({
        "baseline_run_id": baseline_row["run_id"],
        "candidate_run_id": run_id,
        "regressed": any(r.regressed for r in results),
        # Both of these change what the comparison *means*, so they travel
        # with it rather than being separate lookups a caller might skip: a
        # delta across two app builds measures the app, and a delta from an
        # unverified run measures a screen the scenario never reached.
        "app_version": app_version_delta(storage.get_run(baseline_row["run_id"]), run),
        "candidate_quality": storage.run_quality(run_id),
        "metrics": [asdict(r) for r in results],
    })
