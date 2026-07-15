from __future__ import annotations

import json
from dataclasses import asdict

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from autoperf.adb import AdbClient
from autoperf.analyzer import compare, compute_stats

from .services import get_storage, trigger_run


@require_http_methods(["GET"])
def devices(request):
    return JsonResponse(get_storage().list_devices(), safe=False)


@csrf_exempt
@require_http_methods(["POST"])
def devices_refresh(request):
    storage = get_storage()
    found = AdbClient().devices()
    for device in found:
        storage.register_device(device)
    return JsonResponse(storage.list_devices(), safe=False)


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

    run_id = trigger_run(get_storage(), serial, duration)
    return JsonResponse({"run_id": run_id, "status": "pending"}, status=202)


@require_http_methods(["GET"])
def run_detail(request, run_id):
    run = get_storage().get_run(run_id)
    if run is None:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse(run)


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
        result = storage.get_baseline(serial)
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
    return JsonResponse(storage.get_baseline(serial))


@require_http_methods(["GET"])
def run_comparison(request, run_id):
    storage = get_storage()
    run = storage.get_run(run_id)
    if run is None:
        return JsonResponse({"error": "not found"}, status=404)
    baseline_row = storage.get_baseline(run["device_serial"])
    if baseline_row is None:
        return JsonResponse({"error": f"no baseline set for device {run['device_serial']}"}, status=404)

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
