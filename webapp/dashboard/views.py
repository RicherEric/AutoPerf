from __future__ import annotations

import json
from dataclasses import asdict

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from autoperf.adb import AdbClient, AdbError
from autoperf.analyzer import compare, compute_stats
from autoperf.scenarios import youtube as youtube_scenarios

from .services import (
    cancel_run,
    get_dashboard_stats,
    get_queue_status,
    get_storage,
    refresh_devices,
    trigger_run,
    trigger_suite,
)


@require_http_methods(["GET"])
def devices(request):
    return JsonResponse(get_storage().list_devices(), safe=False)


@csrf_exempt
@require_http_methods(["POST"])
def devices_refresh(request):
    return JsonResponse(refresh_devices(get_storage(), AdbClient()), safe=False)


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
        return JsonResponse(get_storage().list_runs(), safe=False)

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

    run_id = trigger_run(get_storage(), serial, duration, youtube_scenario)
    return JsonResponse({"run_id": run_id, "status": "pending"}, status=202)


@csrf_exempt
@require_http_methods(["GET", "DELETE"])
def run_detail(request, run_id):
    storage = get_storage()
    run = storage.get_run(run_id)
    if run is None:
        return JsonResponse({"error": "not found"}, status=404)

    if request.method == "DELETE":
        if run["status"] in ("pending", "running"):
            return JsonResponse({"error": f"run is still {run['status']} -- cannot delete an in-progress run"}, status=400)
        baseline_row = storage.get_baseline(run["device_serial"], run["youtube_scenario"])
        if baseline_row is not None and baseline_row["run_id"] == run_id:
            return JsonResponse(
                {"error": "this run is the current baseline for its device -- set a different baseline first"},
                status=400,
            )
        storage.delete_run(run_id)
        return JsonResponse({"deleted": run_id})

    return JsonResponse(run)


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


@require_http_methods(["GET"])
def queue_status(request):
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
    baseline_stats = compute_stats(storage.list_samples(baseline_row["run_id"], limit=100_000))
    candidate_stats = compute_stats(storage.list_samples(run_id, limit=100_000))
    results = compare(baseline_stats, candidate_stats, threshold_pct=threshold_pct)
    return JsonResponse({
        "baseline_run_id": baseline_row["run_id"],
        "candidate_run_id": run_id,
        "regressed": any(r.regressed for r in results),
        "metrics": [asdict(r) for r in results],
    })
