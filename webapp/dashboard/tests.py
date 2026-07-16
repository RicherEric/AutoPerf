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

    def test_youtube_scenarios_list_returns_name_description_tier(self):
        response = self.client.get("/api/youtube-scenarios")
        self.assertEqual(response.status_code, 200)
        entries = response.json()
        self.assertGreaterEqual(len(entries), 15)
        cold_start = next(e for e in entries if e["name"] == "cold_start")
        self.assertEqual(cold_start["tier"], "smoke")
        self.assertTrue(cold_start["description"])

    def test_youtube_scenarios_list_filters_by_tier(self):
        response = self.client.get("/api/youtube-scenarios?tier=smoke")
        self.assertEqual(response.status_code, 200)
        entries = response.json()
        self.assertTrue(entries)
        self.assertTrue(all(e["tier"] == "smoke" for e in entries))

    def test_youtube_scenarios_list_rejects_unknown_tier(self):
        response = self.client.get("/api/youtube-scenarios?tier=not-a-tier")
        self.assertEqual(response.status_code, 400)

    def test_suites_post_requires_serial(self):
        response = self.client.post(
            "/api/suites", data=json.dumps({"tier": "smoke"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_suites_post_rejects_unknown_tier(self):
        response = self.client.post(
            "/api/suites", data=json.dumps({"serial": "S1", "tier": "nonsense"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.services.run_test_task")
    def test_suites_post_enqueues_one_run_per_scenario_in_tier(self, mock_task):
        response = self.client.post(
            "/api/suites",
            data=json.dumps({"serial": "S1", "tier": "smoke", "duration": 10}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["tier"], "smoke")
        self.assertEqual(payload["count"], 4)
        self.assertEqual(len(payload["run_ids"]), 4)
        self.assertEqual(mock_task.delay.call_count, 4)

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
        mock_task.delay.assert_called_once_with(self.storage.path, "S1", 30, run_id, None)

    @patch("dashboard.services.run_test_task")
    def test_trigger_run_passes_youtube_scenario_through(self, mock_task):
        run_id = trigger_run(self.storage, "S1", 30, "cold_start")
        mock_task.delay.assert_called_once_with(self.storage.path, "S1", 30, run_id, "cold_start")

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

    @patch("dashboard.tasks.AdbClient")
    def test_run_test_task_with_youtube_scenario_drives_adapter(self, mock_adb_client):
        mock_adb_client.return_value.shell.side_effect = lambda serial, command, timeout=10: {
            "dumpsys cpuinfo": "1.0% TOTAL: 1.0% user + 0.0% kernel",
            "cat /proc/meminfo": "MemTotal: 100 kB\nMemAvailable: 50 kB\n",
            "dumpsys battery": " level: 50\n temperature: 300\n",
            "wm size": "Physical size: 1080x2340\n",
            "monkey -p com.google.android.youtube -c android.intent.category.LAUNCHER 1": "",
        }[command]
        run_test_task(str(self.db_path), "S1", 0.05, "run1", "cold_start")
        self.assertEqual(self.storage.get_run("run1")["status"], "completed")
        conn = self.storage.connect()
        try:
            kinds = {row[0] for row in conn.execute("SELECT kind FROM test_events WHERE run_id=?", ("run1",))}
        finally:
            conn.close()
        self.assertIn("adapter_action", kinds)

    @patch("dashboard.services.celery_app")
    def test_queue_status_reports_online_workers(self, mock_celery_app):
        inspector = mock_celery_app.control.inspect.return_value
        inspector.active.return_value = {"worker1@host": [{"id": "abc", "name": "dashboard.run_test"}]}
        inspector.reserved.return_value = {"worker1@host": []}
        inspector.scheduled.return_value = {}

        response = self.client.get("/api/queue")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["broker_reachable"])
        self.assertTrue(payload["worker_online"])
        self.assertEqual(payload["workers"], [
            {"name": "worker1@host", "active": [{"id": "abc", "name": "dashboard.run_test"}], "reserved": [], "scheduled": []}
        ])

    @patch("dashboard.services.celery_app")
    def test_queue_status_reports_no_worker_online_as_a_normal_state(self, mock_celery_app):
        inspector = mock_celery_app.control.inspect.return_value
        inspector.active.return_value = None
        inspector.reserved.return_value = None
        inspector.scheduled.return_value = None

        response = self.client.get("/api/queue")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["broker_reachable"])
        self.assertFalse(payload["worker_online"])
        self.assertEqual(payload["workers"], [])

    @patch("dashboard.services.celery_app")
    def test_queue_status_reports_broker_unreachable_distinctly(self, mock_celery_app):
        mock_celery_app.control.inspect.side_effect = OSError("connection refused")

        response = self.client.get("/api/queue")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["broker_reachable"])
        self.assertFalse(payload["worker_online"])
        self.assertIn("connection refused", payload["error"])

    @patch("dashboard.services.celery_app")
    def test_queue_status_reports_running_runs_from_storage_even_when_celery_sees_nothing(self, mock_celery_app):
        # This is the solo-pool blind spot: the worker is fully synchronous, so
        # it cannot answer an inspect() broadcast while busy executing a task
        # -- active/reserved/scheduled all come back empty even though a run
        # is genuinely in progress. running_runs must still show it, since it
        # reads Storage directly rather than going through Celery at all.
        inspector = mock_celery_app.control.inspect.return_value
        inspector.active.return_value = {}
        inspector.reserved.return_value = {}
        inspector.scheduled.return_value = {}

        self.storage.create_run("run1", "S1")
        self.storage.update_run("run1", "running", checkpoint="12.3")

        response = self.client.get("/api/queue")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["worker_online"])
        self.assertEqual([r["id"] for r in payload["running_runs"]], ["run1"])

    def _complete_run_with_samples(self, run_id, serial, values, scenario=None):
        self.storage.create_run(run_id, serial, scenario)
        writer = BatchWriter(self.storage)
        writer.start()
        for value in values:
            writer.put(MetricSample(run_id, "cpu", "cpu.total", value, "%"))
        writer.close()
        self.storage.update_run(run_id, "completed")

    def test_stats_with_no_runs_returns_zeroed_counts(self):
        response = self.client.get("/api/stats")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total_runs"], 0)
        self.assertIsNone(payload["pass_rate"])
        self.assertEqual(payload["by_scenario"], [])

    def test_stats_counts_no_baseline_runs_separately(self):
        self._complete_run_with_samples("run1", "S1", [10.0])
        response = self.client.get("/api/stats")
        payload = response.json()
        self.assertEqual(payload["no_baseline"], 1)
        self.assertEqual(payload["passed"], 0)
        self.assertEqual(payload["failed"], 0)
        self.assertIsNone(payload["pass_rate"])

    def test_stats_marks_run_within_threshold_as_pass(self):
        self._complete_run_with_samples("baseline_run", "S1", [10.0])
        self.storage.set_baseline("S1", "baseline_run")
        self._complete_run_with_samples("run1", "S1", [10.5], scenario="cold_start")

        response = self.client.get("/api/stats")
        payload = response.json()
        self.assertEqual(payload["passed"], 2)  # baseline run compares against itself too
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(payload["pass_rate"], 1.0)

    def test_stats_marks_regressed_run_as_fail(self):
        self._complete_run_with_samples("baseline_run", "S1", [10.0])
        self.storage.set_baseline("S1", "baseline_run")
        self._complete_run_with_samples("run1", "S1", [50.0], scenario="cold_start")

        response = self.client.get("/api/stats")
        payload = response.json()
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["passed"], 1)  # the baseline run itself still passes

    def test_stats_explains_why_a_run_failed(self):
        self._complete_run_with_samples("baseline_run", "S1", [10.0])
        self.storage.set_baseline("S1", "baseline_run")
        self._complete_run_with_samples("run1", "S1", [50.0], scenario="cold_start")

        response = self.client.get("/api/stats")
        payload = response.json()
        self.assertEqual(payload["threshold_pct"], 20.0)
        failed_run = next(r for r in payload["runs"] if r["run_id"] == "run1")
        self.assertEqual(failed_run["verdict"], "fail")
        self.assertEqual(failed_run["baseline_run_id"], "baseline_run")
        self.assertEqual(failed_run["regressed_metrics"], [{"name": "cpu.total", "delta_pct": 400.0}])

        passing_run = next(r for r in payload["runs"] if r["run_id"] == "baseline_run")
        self.assertEqual(passing_run["verdict"], "pass")
        self.assertEqual(passing_run["regressed_metrics"], [])

    def test_stats_groups_by_scenario(self):
        self._complete_run_with_samples("baseline_run", "S1", [10.0])
        self.storage.set_baseline("S1", "baseline_run")
        self._complete_run_with_samples("run1", "S1", [10.5], scenario="cold_start")
        self._complete_run_with_samples("run2", "S1", [50.0], scenario="like_video")

        response = self.client.get("/api/stats")
        payload = response.json()
        by_scenario = {entry["scenario"]: entry for entry in payload["by_scenario"]}
        self.assertEqual(by_scenario["cold_start"]["pass"], 1)
        self.assertEqual(by_scenario["like_video"]["fail"], 1)

    def test_stats_includes_metric_trend(self):
        self._complete_run_with_samples("run1", "S1", [10.0])
        response = self.client.get("/api/stats")
        payload = response.json()
        self.assertIn("cpu.total", payload["trend"])
        self.assertEqual(payload["trend"]["cpu.total"][0]["value"], 10.0)

    def test_stats_respects_limit_query_param(self):
        self._complete_run_with_samples("run1", "S1", [10.0])
        self._complete_run_with_samples("run2", "S1", [20.0])
        response = self.client.get("/api/stats?limit=1")
        payload = response.json()
        self.assertEqual(payload["total_runs"], 1)
