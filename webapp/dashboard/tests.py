import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from django.test import Client, SimpleTestCase, override_settings

from autoperf.adb import AdbError
from autoperf.models import Device, MetricSample
from autoperf.runner import DeviceBusyError
from autoperf.storage import BatchWriter, Storage
from dashboard.services import trigger_run
from dashboard.tasks import DEVICE_BUSY_RETRY_COUNTDOWN, run_test_task


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
        mock_adb_client.return_value.shell.side_effect = RuntimeError("device offline")
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
        self.assertEqual(mock_task.apply_async.call_count, 4)

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
        mock_task.apply_async.assert_called_once_with(
            args=[self.storage.path, "S1", 30, run_id, None], task_id=run_id
        )

    @patch("dashboard.services.run_test_task")
    def test_trigger_run_passes_youtube_scenario_through(self, mock_task):
        run_id = trigger_run(self.storage, "S1", 30, "cold_start")
        mock_task.apply_async.assert_called_once_with(
            args=[self.storage.path, "S1", 30, run_id, "cold_start"], task_id=run_id
        )

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
        # Baseline must share the candidate's scenario -- baselines are
        # scoped per (device, scenario) precisely so a heavier/lighter
        # scenario's naturally-different resource usage is never mistaken
        # for a regression (see Storage.get_baseline's docstring).
        self._complete_run_with_samples("baseline_run", "S1", [10.0], scenario="cold_start")
        self.storage.set_baseline("S1", "baseline_run")
        self._complete_run_with_samples("run1", "S1", [10.5], scenario="cold_start")

        response = self.client.get("/api/stats")
        payload = response.json()
        self.assertEqual(payload["passed"], 2)  # baseline run compares against itself too
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(payload["pass_rate"], 1.0)

    def test_stats_marks_regressed_run_as_fail(self):
        self._complete_run_with_samples("baseline_run", "S1", [10.0], scenario="cold_start")
        self.storage.set_baseline("S1", "baseline_run")
        self._complete_run_with_samples("run1", "S1", [50.0], scenario="cold_start")

        response = self.client.get("/api/stats")
        payload = response.json()
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["passed"], 1)  # the baseline run itself still passes

    def test_stats_explains_why_a_run_failed(self):
        self._complete_run_with_samples("baseline_run", "S1", [10.0], scenario="cold_start")
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
        # Two scenarios each need their own scenario-scoped baseline.
        self._complete_run_with_samples("baseline_cold", "S1", [10.0], scenario="cold_start")
        self.storage.set_baseline("S1", "baseline_cold")
        self._complete_run_with_samples("baseline_like", "S1", [40.0], scenario="like_video")
        self.storage.set_baseline("S1", "baseline_like")
        self._complete_run_with_samples("run1", "S1", [10.5], scenario="cold_start")
        self._complete_run_with_samples("run2", "S1", [50.0], scenario="like_video")

        response = self.client.get("/api/stats")
        payload = response.json()
        by_scenario = {entry["scenario"]: entry for entry in payload["by_scenario"]}
        self.assertEqual(by_scenario["cold_start"]["pass"], 2)  # baseline_cold + run1
        self.assertEqual(by_scenario["like_video"]["fail"], 1)  # run2 regressed vs baseline_like

    def test_baseline_does_not_leak_across_different_scenarios(self):
        # Regression test for a real bug: a device's baseline used to be
        # global, so a heavier scenario compared against a lighter
        # scenario's baseline produced a false "fail" purely because it does
        # more on-screen work, not because anything actually regressed.
        self._complete_run_with_samples("baseline_run", "S1", [10.0], scenario="cold_start")
        self.storage.set_baseline("S1", "baseline_run")
        self._complete_run_with_samples("run1", "S1", [500.0], scenario="multi_video_session")

        response = self.client.get("/api/stats")
        payload = response.json()
        run1_verdict = next(r for r in payload["runs"] if r["run_id"] == "run1")
        self.assertEqual(run1_verdict["verdict"], "no_baseline")

        comparison_response = self.client.get("/api/runs/run1/comparison")
        self.assertEqual(comparison_response.status_code, 404)

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

    def test_stats_filters_by_device_query_param(self):
        self._complete_run_with_samples("run1", "S1", [10.0])
        self._complete_run_with_samples("run2", "S2", [20.0])
        response = self.client.get("/api/stats?device=S1")
        payload = response.json()
        self.assertEqual(payload["total_runs"], 1)
        self.assertEqual(payload["runs"][0]["device_serial"], "S1")

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

    @patch("dashboard.views.AdbClient")
    def test_devices_connect_requires_address(self, mock_adb_client):
        response = self.client.post("/api/devices/connect", data=json.dumps({}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.views.AdbClient")
    def test_devices_connect_success_refreshes_device_list(self, mock_adb_client):
        mock_adb_client.return_value.connect.return_value = "connected to 192.168.1.50:5555"
        mock_adb_client.return_value.devices.return_value = [Device("192.168.1.50:5555", "device", "Pixel", "pixel")]
        mock_adb_client.return_value.shell.side_effect = RuntimeError("skip enrichment in this test")
        response = self.client.post(
            "/api/devices/connect",
            data=json.dumps({"address": "192.168.1.50:5555"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("connected to", payload["message"])
        self.assertEqual({d["serial"] for d in payload["devices"]}, {"192.168.1.50:5555"})

    @patch("dashboard.views.AdbClient")
    def test_devices_connect_reports_adb_failure(self, mock_adb_client):
        mock_adb_client.return_value.connect.side_effect = AdbError("cannot connect to 1.2.3.4:5555")
        response = self.client.post(
            "/api/devices/connect", data=json.dumps({"address": "1.2.3.4:5555"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot connect", response.json()["error"])

    @patch("dashboard.views.AdbClient")
    def test_devices_pair_requires_address_and_code(self, mock_adb_client):
        response = self.client.post(
            "/api/devices/pair", data=json.dumps({"address": "192.168.1.50:37251"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.views.AdbClient")
    def test_devices_pair_success(self, mock_adb_client):
        mock_adb_client.return_value.pair.return_value = "Successfully paired to 192.168.1.50:37251"
        response = self.client.post(
            "/api/devices/pair",
            data=json.dumps({"address": "192.168.1.50:37251", "code": "123456"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("paired", response.json()["message"].lower())

    @patch("dashboard.views.AdbClient")
    def test_devices_pair_reports_failure(self, mock_adb_client):
        mock_adb_client.return_value.pair.side_effect = AdbError("Failed: wrong pairing code")
        response = self.client.post(
            "/api/devices/pair",
            data=json.dumps({"address": "192.168.1.50:37251", "code": "000000"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_device_nickname_sets_and_persists(self):
        self.storage.register_device(Device("S1", "device", "Pixel", "pixel"))
        response = self.client.post(
            "/api/devices/S1/nickname", data=json.dumps({"nickname": "Alice's phone"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        devices = {d["serial"]: d for d in self.client.get("/api/devices").json()}
        self.assertEqual(devices["S1"]["nickname"], "Alice's phone")

    @patch("dashboard.views.AdbClient")
    def test_devices_refresh_labels_wireless_connection_from_serial_shape(self, mock_adb_client):
        mock_adb_client.return_value.devices.return_value = [Device("192.168.1.50:5555", "device", "Pixel", "pixel")]
        mock_adb_client.return_value.shell.side_effect = RuntimeError("skip enrichment in this test")
        response = self.client.post("/api/devices/refresh")
        payload = response.json()
        self.assertEqual(payload[0]["connection"], "wifi")

    @patch("dashboard.views.AdbClient")
    def test_devices_refresh_parses_extended_identity_info(self, mock_adb_client):
        # Realistic mocked output shaped like real `adb shell` responses,
        # captured against a real Galaxy A55 during development.
        mock_adb_client.return_value.devices.return_value = [Device("S1", "device", "SM_A5560", "a55xzh")]
        mock_adb_client.return_value.shell.side_effect = lambda serial, command, timeout=10: {
            "getprop": (
                "[ro.build.version.release]: [14]\n"
                "[ro.product.manufacturer]: [samsung]\n"
                "[ro.product.brand]: [samsung]\n"
                "[ro.build.version.sdk]: [34]\n"
                "[ro.build.display.id]: [UP1A.231005.007.A5560ZHS7AYC6]\n"
                "[ro.product.cpu.abi]: [arm64-v8a]\n"
            ),
            "dumpsys battery": " level: 79\n temperature: 300\n",
            "settings get global device_name": "Galaxy A55 5G\n",
            "dumpsys package com.android.chrome": "    versionName=150.0.7871.114\n",
            "ip -f inet addr show wlan0": "    inet 192.168.1.217/24 brd 192.168.1.255 scope global wlan0\n",
        }[command]

        response = self.client.post("/api/devices/refresh")
        payload = response.json()[0]
        self.assertEqual(payload["android_version"], "14")
        self.assertEqual(payload["nickname"], "Galaxy A55 5G")
        self.assertEqual(payload["manufacturer"], "samsung")
        self.assertEqual(payload["brand"], "samsung")
        self.assertEqual(payload["sdk_version"], "34")
        self.assertEqual(payload["build_id"], "UP1A.231005.007.A5560ZHS7AYC6")
        self.assertEqual(payload["cpu_abi"], "arm64-v8a")
        self.assertEqual(payload["chrome_version"], "150.0.7871.114")
        self.assertEqual(payload["wifi_ip"], "192.168.1.217")
        self.assertIn("Android 14", payload["user_agent"])
        self.assertIn("SM_A5560", payload["user_agent"])
        self.assertIn("Chrome/150.0.7871.114", payload["user_agent"])

    @patch("dashboard.views.AdbClient")
    def test_devices_refresh_omits_user_agent_when_build_id_unavailable(self, mock_adb_client):
        mock_adb_client.return_value.devices.return_value = [Device("S1", "device", "Pixel", "pixel")]
        mock_adb_client.return_value.shell.side_effect = RuntimeError("device unresponsive")

        response = self.client.post("/api/devices/refresh")
        payload = response.json()[0]
        self.assertIsNone(payload["user_agent"])
        self.assertIsNone(payload["manufacturer"])

    @patch("dashboard.views.AdbClient")
    def test_device_nickname_survives_a_later_refresh(self, mock_adb_client):
        # register_device's COALESCE keeps a manually-set nickname even when
        # a fresh refresh's auto-detected device_name would otherwise fill it.
        self.storage.register_device(Device("S1", "device", "Pixel", "pixel"))
        self.storage.set_device_nickname("S1", "Eric's phone")

        mock_adb_client.return_value.devices.return_value = [Device("S1", "device", "Pixel", "pixel")]
        mock_adb_client.return_value.shell.side_effect = lambda serial, command, timeout=10: {
            "getprop": "",
            "dumpsys battery": " level: 50\n",
            "settings get global device_name": "Some Other Name\n",
            "dumpsys package com.android.chrome": "",
            "ip -f inet addr show wlan0": "",
        }[command]

        response = self.client.post("/api/devices/refresh")
        self.assertEqual(response.json()[0]["nickname"], "Eric's phone")
