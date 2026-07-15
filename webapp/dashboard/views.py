from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from autoperf.adb import AdbClient

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
