import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import Client, SimpleTestCase, override_settings

from autoperf.models import Device, MetricSample
from autoperf.storage import BatchWriter, Storage
from dashboard.services import trigger_run
from dashboard.tasks import run_test_task


class DashboardApiTests(SimpleTestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tempdir.name) / "test.db"
        self.storage = Storage(self.db_path)
        self.storage.initialize()
        self._settings_override = override_settings(AUTOPERF_DB_PATH=self.db_path)
        self._settings_override.enable()
        self.client = Client()

    def tearDown(self):
        self._settings_override.disable()
        self._tempdir.cleanup()

    def test_devices_lists_registered_devices(self):
        self.storage.register_device(Device("S1", "device", "Pixel", "pixel"))
        response = self.client.get("/api/devices")
        self.assertEqual(response.status_code, 200)
        serials = {d["serial"] for d in response.json()}
        self.assertEqual(serials, {"S1"})

    @patch("dashboard.views.AdbClient")
    def test_devices_refresh_registers_and_returns_devices(self, mock_adb_client):
        mock_adb_client.return_value.devices.return_value = [Device("S2", "device", "Galaxy", "galaxy")]
        response = self.client.post("/api/devices/refresh")
        self.assertEqual(response.status_code, 200)
        serials = {d["serial"] for d in response.json()}
        self.assertEqual(serials, {"S2"})

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
        self.assertEqual(mock_trigger.call_args.args[1:], ("S1", 5.0))

    def test_run_detail_returns_404_for_missing_run(self):
        response = self.client.get("/api/runs/missing-run")
        self.assertEqual(response.status_code, 404)

    def test_run_detail_returns_run(self):
        self.storage.create_run("run1", "S1")
        response = self.client.get("/api/runs/run1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "run1")

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

    def test_baseline_get_returns_404_when_unset(self):
        response = self.client.get("/api/devices/S1/baseline")
        self.assertEqual(response.status_code, 404)

    def test_baseline_post_rejects_run_from_different_device(self):
        self.storage.create_run("run1", "OTHER")
        response = self.client.post(
            "/api/devices/S1/baseline", data=json.dumps({"run_id": "run1"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_baseline_post_then_get_returns_baseline(self):
        self.storage.create_run("run1", "S1")
        response = self.client.post(
            "/api/devices/S1/baseline", data=json.dumps({"run_id": "run1"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_id"], "run1")

        response = self.client.get("/api/devices/S1/baseline")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_id"], "run1")

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

    @patch("dashboard.services.run_test_task")
    def test_trigger_run_creates_pending_row_and_enqueues_celery_task(self, mock_task):
        run_id = trigger_run(self.storage, "S1", 30)
        self.assertEqual(self.storage.get_run(run_id)["status"], "pending")
        mock_task.delay.assert_called_once_with(self.storage.path, "S1", 30, run_id)

    @patch("dashboard.tasks.AdbClient")
    def test_run_test_task_executes_directly_and_completes(self, mock_adb_client):
        mock_adb_client.return_value.shell.side_effect = lambda serial, command, timeout=10: {
            "dumpsys cpuinfo": "1.0% TOTAL: 1.0% user + 0.0% kernel",
            "cat /proc/meminfo": "MemTotal: 100 kB\nMemAvailable: 50 kB\n",
            "dumpsys battery": " level: 50\n temperature: 300\n",
        }[command]
        # Calling the task function directly (not .delay()) runs its body
        # synchronously in-process, with no Celery broker/worker involved --
        # the standard way to unit test a task's logic in isolation.
        run_test_task(str(self.db_path), "S1", 0.05, "run1")
        self.assertEqual(self.storage.get_run("run1")["status"], "completed")
