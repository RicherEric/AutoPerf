"""Stats, baselines and comparison.

Split out of one 1120-line module; the shared setUp lives in support.ApiTestCase.
"""

from contextlib import closing
import json

from autoperf.models import MetricSample
from autoperf.storage import BatchWriter, Storage

from dashboard.tests.support import ApiTestCase


class AnalysisApiTests(ApiTestCase):
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

    def test_stats_includes_metric_trend_for_one_device(self):
        self._complete_run_with_samples("run1", "S1", [10.0])
        response = self.client.get("/api/stats?device=S1")
        payload = response.json()
        self.assertIn("cpu.total", payload["trend"])
        self.assertEqual(payload["trend"]["cpu.total"][0]["value"], 10.0)
        self.assertEqual(payload["trend_scope"], "device")

    def test_stats_omits_the_trend_when_devices_are_combined(self):
        # One line through two machines' means is not a trend, it is two
        # unrelated series drawn as one -- the page says so instead.
        self._complete_run_with_samples("run1", "S1", [10.0])
        self._complete_run_with_samples("run2", "S2", [90.0])

        payload = self.client.get("/api/stats").json()

        self.assertEqual(payload["trend"], {})
        self.assertEqual(payload["trend_scope"], "all_devices")

    def test_the_trend_is_ordered_by_when_runs_actually_ran(self):
        # Campaign children are all created up front, so row order says when
        # they were planned. Plotted against that, the line doubles back.
        self._complete_run_with_samples("later", "S1", [20.0])
        self._complete_run_with_samples("earlier", "S1", [10.0])
        self.storage.update_run("later", "completed")
        with closing(self.storage.connect()) as conn:
            with conn:
                conn.execute("UPDATE test_runs SET started_at=? WHERE id=?",
                             ("2026-01-01T00:00:00+00:00", "earlier"))
                conn.execute("UPDATE test_runs SET started_at=? WHERE id=?",
                             ("2026-01-02T00:00:00+00:00", "later"))

        points = self.client.get("/api/stats?device=S1").json()["trend"]["cpu.total"]

        self.assertEqual([p["value"] for p in points], [10.0, 20.0])

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
