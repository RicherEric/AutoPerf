"""Starting, reading, cancelling and listing runs.

Split out of one 1120-line module; the shared setUp lives in support.ApiTestCase.
"""

import json
import threading
import time
from unittest.mock import patch

from autoperf.models import MetricSample
from autoperf.runner import DeviceBusyError
from autoperf.storage import BatchWriter
from dashboard.services import trigger_run
from dashboard.tasks import DEVICE_BUSY_RETRY_COUNTDOWN, run_test_task

from dashboard.tests.support import ApiTestCase


class RunApiTests(ApiTestCase):
    def test_runs_get_lists_runs(self):
        self.storage.create_run("run1", "S1")
        response = self.client.get("/api/runs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["id"] for r in response.json()], ["run1"])

    def test_runs_post_without_serial_returns_400(self):
        response = self.client.post("/api/runs", data=json.dumps({}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.views.trigger_run", return_value="new-run-id")
    def test_runs_post_triggers_run_and_returns_202(self, mock_trigger):
        response = self.client.post(
            "/api/runs", data=json.dumps({"serial": "S1", "duration": 5}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"run_id": "new-run-id", "status": "pending"})
        mock_trigger.assert_called_once()
        self.assertEqual(mock_trigger.call_args.args[1:], ("S1", 5.0, None))

    def test_runs_post_rejects_unknown_youtube_scenario(self):
        response = self.client.post(
            "/api/runs",
            data=json.dumps({"serial": "S1", "youtube_scenario": "not-a-real-scenario"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.views.trigger_run", return_value="new-run-id")
    def test_runs_post_passes_youtube_scenario_through(self, mock_trigger):
        response = self.client.post(
            "/api/runs",
            data=json.dumps({"serial": "S1", "duration": 5, "youtube_scenario": "cold_start"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(mock_trigger.call_args.args[1:], ("S1", 5.0, "cold_start"))

    def test_run_detail_returns_404_for_missing_run(self):
        response = self.client.get("/api/runs/missing-run")
        self.assertEqual(response.status_code, 404)

    def test_run_detail_returns_run(self):
        self.storage.create_run("run1", "S1")
        response = self.client.get("/api/runs/run1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "run1")

    def test_run_detail_delete_removes_a_completed_run(self):
        self.storage.create_run("run1", "S1")
        self.storage.update_run("run1", "completed")
        response = self.client.delete("/api/runs/run1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": "run1"})
        self.assertIsNone(self.storage.get_run("run1"))

    def test_run_detail_delete_removes_the_recording_file_too(self):
        self.storage.create_run("run1", "S1")
        self.storage.update_run("run1", "completed")
        self.recordings_root.mkdir(parents=True)
        recording = self.recordings_root / "run1.mp4"
        recording.write_bytes(b"fake mp4 bytes")
        response = self.client.delete("/api/runs/run1")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(recording.exists())

    def test_run_recording_returns_not_exists_when_no_file(self):
        self.storage.create_run("run1", "S1")
        response = self.client.get("/api/runs/run1/recording")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"exists": False, "url": None})

    def test_run_recording_returns_exists_and_url_once_file_present(self):
        self.storage.create_run("run1", "S1")
        self.recordings_root.mkdir(parents=True)
        (self.recordings_root / "run1.mp4").write_bytes(b"fake mp4 bytes")
        response = self.client.get("/api/runs/run1/recording")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"exists": True, "url": "/recordings/run1.mp4"})

    def test_run_recording_returns_404_for_missing_run(self):
        response = self.client.get("/api/runs/missing-run/recording")
        self.assertEqual(response.status_code, 404)

    def test_run_detail_delete_returns_404_for_missing_run(self):
        response = self.client.delete("/api/runs/missing-run")
        self.assertEqual(response.status_code, 404)

    def test_run_detail_delete_rejects_running_run(self):
        self.storage.create_run("run1", "S1")
        self.storage.update_run("run1", "running")
        response = self.client.delete("/api/runs/run1")
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(self.storage.get_run("run1"))

    def test_run_detail_delete_rejects_current_baseline(self):
        self.storage.create_run("run1", "S1")
        self.storage.update_run("run1", "completed")
        self.storage.set_baseline("S1", "run1")
        response = self.client.delete("/api/runs/run1")
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(self.storage.get_run("run1"))

    def test_run_samples_filters_by_since_id_and_decodes_labels(self):
        writer = BatchWriter(self.storage)
        writer.start()
        writer.put(MetricSample("run1", "cpu", "cpu.total", 1.0, "%"))
        writer.put(MetricSample("run1", "cpu", "cpu.total", 2.0, "%"))
        writer.close()

        response = self.client.get("/api/runs/run1/samples")
        payload = response.json()
        self.assertEqual([s["value"] for s in payload["samples"]], [1.0, 2.0])
        self.assertEqual(payload["samples"][0]["labels"], {})

        first_id = payload["samples"][0]["id"]
        response = self.client.get(f"/api/runs/run1/samples?since_id={first_id}")
        payload = response.json()
        self.assertEqual([s["value"] for s in payload["samples"]], [2.0])

    def test_run_comparison_returns_404_without_baseline(self):
        self.storage.create_run("run1", "S1")
        response = self.client.get("/api/runs/run1/comparison")
        self.assertEqual(response.status_code, 404)

    def test_run_comparison_flags_regression_against_baseline(self):
        writer = BatchWriter(self.storage)
        writer.start()
        writer.put(MetricSample("baseline_run", "cpu", "cpu.total", 10.0, "%"))
        writer.put(MetricSample("candidate_run", "cpu", "cpu.total", 50.0, "%"))
        writer.close()
        self.storage.create_run("baseline_run", "S1")
        self.storage.create_run("candidate_run", "S1")
        self.storage.set_baseline("S1", "baseline_run")

        response = self.client.get("/api/runs/candidate_run/comparison")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["regressed"])
        self.assertEqual(payload["metrics"][0]["name"], "cpu.total")

    @patch("dashboard.tasks.AdbClient")
    def test_run_test_task_executes_directly_and_completes(self, mock_adb_client):
        mock_adb_client.return_value.shell.side_effect = lambda serial, command, timeout=10: {
            "getprop ro.build.characteristics": "phone",
            "dumpsys cpuinfo": "1.0% TOTAL: 1.0% user + 0.0% kernel",
            "cat /proc/meminfo": "MemTotal: 100 kB\nMemAvailable: 50 kB\n",
            "dumpsys battery": " level: 50\n temperature: 300\n",
        }[command]
        # Calling the task function directly (not .delay()) runs its body
        # synchronously in-process, with no Celery broker/worker involved --
        # the standard way to unit test a task's logic in isolation.
        run_test_task(str(self.db_path), "S1", 0.05, "run1")
        self.assertEqual(self.storage.get_run("run1")["status"], "completed")

    @patch.object(run_test_task, "retry")
    def test_run_test_task_retries_when_device_is_busy(self, mock_retry):
        # A bound task's retry() always raises internally (Retry or
        # MaxRetriesExceededError) -- the mock mirrors that so
        # `raise self.retry(...)` in tasks.py behaves the same way here as
        # it would against a real broker, without needing one.
        mock_retry.side_effect = RuntimeError("retry requested")
        self.storage.create_run("run1", "device")
        self.storage.try_start_run("run1")
        self.storage.create_run("run2", "device")
        with self.assertRaises(RuntimeError):
            run_test_task(str(self.db_path), "device", 0.05, "run2")
        _, kwargs = mock_retry.call_args
        self.assertIsInstance(kwargs["exc"], DeviceBusyError)
        self.assertEqual(kwargs["countdown"], DEVICE_BUSY_RETRY_COUNTDOWN)
        self.assertEqual(self.storage.get_run("run2")["status"], "pending")

    @patch("dashboard.tasks.AdbClient")
    def test_run_test_task_with_youtube_scenario_drives_adapter(self, mock_adb_client):
        mock_adb_client.return_value.shell.side_effect = lambda serial, command, timeout=10: {
            "getprop ro.build.characteristics": "phone",
            "dumpsys cpuinfo": "1.0% TOTAL: 1.0% user + 0.0% kernel",
            "cat /proc/meminfo": "MemTotal: 100 kB\nMemAvailable: 50 kB\n",
            "dumpsys battery": " level: 50\n temperature: 300\n",
            "wm size": "Physical size: 1080x2340\n",
            "monkey -p com.google.android.youtube -c android.intent.category.LAUNCHER 1": "",
        }[command]
        run_test_task(str(self.db_path), "S1", 0.2, "run1", "cold_start")
        self.assertEqual(self.storage.get_run("run1")["status"], "completed")
        self.assertEqual(self.storage.get_baseline("S1", "cold_start")["run_id"], "run1")
        conn = self.storage.connect()
        try:
            kinds = {row[0] for row in conn.execute("SELECT kind FROM test_events WHERE run_id=?", ("run1",))}
        finally:
            conn.close()
        self.assertIn("adapter_action", kinds)

    def test_run_cancel_returns_404_for_missing_run(self):
        response = self.client.post("/api/runs/missing-run/cancel")
        self.assertEqual(response.status_code, 404)

    def test_run_cancel_rejects_already_terminal_run(self):
        self.storage.create_run("run1", "S1")
        self.storage.update_run("run1", "completed")
        response = self.client.post("/api/runs/run1/cancel")
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.services.celery_app")
    def test_run_cancel_revokes_and_marks_interrupted_when_pending(self, mock_celery_app):
        # revoke() is fired in a background thread and must never block this
        # response -- see _revoke_in_background's docstring for why (an
        # empirically observed real hang on a busy --pool=solo worker).
        revoked = threading.Event()
        mock_celery_app.control.revoke.side_effect = lambda *a, **k: revoked.set()

        self.storage.create_run("run1", "S1")
        response = self.client.post("/api/runs/run1/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "interrupted")
        self.assertEqual(self.storage.get_run("run1")["status"], "interrupted")

        self.assertTrue(revoked.wait(timeout=1), "revoke() was never called in the background")
        mock_celery_app.control.revoke.assert_called_once_with("run1")

    @patch("dashboard.services.celery_app")
    def test_run_cancel_returns_immediately_even_if_revoke_hangs(self, mock_celery_app):
        # Directly guards against the regression this fix addresses: even if
        # the broker round-trip inside revoke() blocks for a long time, the
        # HTTP response must come back right away.
        release = threading.Event()
        mock_celery_app.control.revoke.side_effect = lambda *a, **k: release.wait(timeout=5)

        self.storage.create_run("run1", "S1")
        started = time.monotonic()
        response = self.client.post("/api/runs/run1/cancel")
        elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 1.0)
        release.set()

    def test_run_cancel_sets_cancel_flag_when_running(self):
        self.storage.create_run("run1", "S1")
        self.storage.update_run("run1", "running")
        response = self.client.post("/api/runs/run1/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelling")
        self.assertEqual(self.storage.get_run("run1")["cancel_requested"], 1)
        # still running -- TestRunner's own loop is responsible for winding down
        self.assertEqual(self.storage.get_run("run1")["status"], "running")
