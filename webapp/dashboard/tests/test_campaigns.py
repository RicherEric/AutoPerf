"""Campaign endpoints.

`run_test_task` is patched for the whole class: these assert on what a campaign
*enqueues* and how it reads back afterwards, never on running a real device.
Every test therefore takes a `task` argument.
"""

import json
from unittest.mock import patch

from autoperf.models import MetricSample
from autoperf.scenarios.youtube import list_scenarios
from autoperf.storage import BatchWriter

from dashboard.tests.support import ApiTestCase


@patch("dashboard.services.run_test_task")
class CampaignApiTests(ApiTestCase):
    def _post(self, payload):
        return self.client.post("/api/campaigns", data=json.dumps(payload),
                                content_type="application/json")

    def _write_samples(self, run_id, name, values, unit="%", timestamps=None):
        writer = BatchWriter(self.storage)
        writer.start()
        for index, value in enumerate(values):
            kwargs = {"timestamp": timestamps[index]} if timestamps else {}
            writer.put(MetricSample(run_id, "collector", name, float(value), unit, **kwargs))
        writer.close()

    def test_repeat_campaign_enqueues_one_run_per_scenario_per_iteration(self, task):
        response = self._post({"kind": "repeat", "serial": "S1", "tier": "smoke",
                               "duration": 30, "iterations": 3})
        self.assertEqual(response.status_code, 202)
        expected = len(list_scenarios(tier="smoke")) * 3
        self.assertEqual(response.json()["count"], expected)
        self.assertEqual(task.apply_async.call_count, expected)

    def test_repeat_over_a_tier_is_ordered_iteration_major(self, task):
        # A campaign cut short must leave an even number of samples per
        # scenario, not all of scenario A and none of the rest.
        response = self._post({"kind": "repeat", "serial": "S1", "tier": "smoke",
                               "duration": 30, "iterations": 2})
        runs = self.storage.list_campaign_runs(response.json()["campaign_id"])
        names = [run["youtube_scenario"] for run in runs]
        tier = list_scenarios(tier="smoke")
        self.assertEqual(names, tier + tier)

    def test_soak_campaign_is_always_a_single_run(self, task):
        response = self._post({"kind": "soak", "serial": "S1", "scenario": "play_golden",
                               "duration": 3600, "iterations": 50})
        self.assertEqual(response.json()["count"], 1)
        campaign = self.storage.get_campaign(response.json()["campaign_id"])
        self.assertEqual(campaign["iterations"], 1)

    def test_rejects_invalid_requests(self, task):
        cases = [
            ({"kind": "nope", "serial": "S1", "duration": 30}, "kind must be one of"),
            ({"kind": "repeat", "serial": "S1", "duration": 30}, "needs either a scenario or a tier"),
            ({"kind": "soak", "serial": "S1", "duration": 0}, "duration must be positive"),
            ({"kind": "soak", "serial": "", "duration": 30}, "serial is required"),
            ({"kind": "repeat", "serial": "S1", "duration": 30, "tier": "nope", "iterations": 1},
             "tier must be one of"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                response = self._post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.json()["error"])
        self.assertEqual(task.apply_async.call_count, 0)

    def test_rejects_a_campaign_larger_than_the_run_cap(self, task):
        response = self._post({"kind": "repeat", "serial": "S1", "tier": "regression",
                               "duration": 30, "iterations": 999})
        self.assertEqual(response.status_code, 400)
        self.assertIn("above the limit", response.json()["error"])
        self.assertEqual(task.apply_async.call_count, 0)

    def test_invalid_json_body_is_rejected(self, task):
        response = self.client.post("/api/campaigns", data=b"{oops",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_detail_reports_flaky_scenarios(self, task):
        response = self._post({"kind": "repeat", "serial": "S1", "scenario": "cold_start",
                               "duration": 30, "iterations": 3})
        campaign_id = response.json()["campaign_id"]
        runs = self.storage.list_campaign_runs(campaign_id)
        # Same device, same scenario, same duration -- one fails anyway.
        for index, run in enumerate(runs):
            self.storage.update_run(run["id"], "failed" if index == 0 else "completed")
            if index:
                self._write_samples(run["id"], "cpu.total", [40, 41, 42])

        detail = self.client.get(f"/api/campaigns/{campaign_id}").json()
        entry = detail["repeat"]["scenarios"][0]
        self.assertEqual(entry["scenario"], "cold_start")
        self.assertTrue(entry["flaky"])
        self.assertEqual(entry["errored"], 1)
        self.assertEqual(entry["completed"], 2)
        self.assertAlmostEqual(entry["pass_rate"], 200 / 3, places=6)

    def test_consistent_scenario_is_not_flagged_flaky(self, task):
        response = self._post({"kind": "repeat", "serial": "S1", "scenario": "cold_start",
                               "duration": 30, "iterations": 3})
        campaign_id = response.json()["campaign_id"]
        for run in self.storage.list_campaign_runs(campaign_id):
            self.storage.update_run(run["id"], "completed")
            self._write_samples(run["id"], "cpu.total", [40, 41, 42])

        entry = self.client.get(f"/api/campaigns/{campaign_id}").json()["repeat"]["scenarios"][0]
        self.assertFalse(entry["flaky"])
        self.assertEqual(entry["pass_rate"], 100.0)

    def test_detail_reports_metric_spread_across_iterations(self, task):
        response = self._post({"kind": "repeat", "serial": "S1", "scenario": "cold_start",
                               "duration": 30, "iterations": 2})
        campaign_id = response.json()["campaign_id"]
        runs = self.storage.list_campaign_runs(campaign_id)
        self.storage.update_run(runs[0]["id"], "completed")
        self._write_samples(runs[0]["id"], "cpu.total", [10, 10, 10])
        self.storage.update_run(runs[1]["id"], "completed")
        self._write_samples(runs[1]["id"], "cpu.total", [30, 30, 30])

        stability = self.client.get(f"/api/campaigns/{campaign_id}").json()
        metric = stability["repeat"]["scenarios"][0]["metric_stability"][0]
        self.assertEqual(metric["name"], "cpu.total")
        self.assertEqual(metric["runs"], 2)
        self.assertAlmostEqual(metric["mean"], 20.0, places=6)
        self.assertAlmostEqual(metric["minimum"], 10.0, places=6)
        self.assertAlmostEqual(metric["maximum"], 30.0, places=6)
        self.assertAlmostEqual(metric["cv_pct"], 50.0, places=6)

    def test_soak_detail_reports_drift(self, task):
        from datetime import datetime, timedelta, timezone

        response = self._post({"kind": "soak", "serial": "S1", "scenario": "play_golden",
                               "duration": 28800})
        campaign_id = response.json()["campaign_id"]
        run_id = self.storage.list_campaign_runs(campaign_id)[0]["id"]
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        count = 200
        values = [2_000_000 + 600_000 * i / (count - 1) for i in range(count)]
        stamps = [(origin + timedelta(seconds=i * 144)).isoformat() for i in range(count)]
        self._write_samples(run_id, "memory.used", values, unit="KiB", timestamps=stamps)
        self.storage.update_run(run_id, "completed")

        soak = self.client.get(f"/api/campaigns/{campaign_id}").json()["soak"]
        self.assertEqual(soak["run_id"], run_id)
        trend = soak["trends"][0]
        self.assertEqual(trend["name"], "memory.used")
        self.assertGreater(trend["drift_pct"], 20)
        self.assertGreater(trend["slope_per_hour"], 0)
        self.assertAlmostEqual(trend["span_hours"], 199 * 144 / 3600, places=3)

    def test_status_is_derived_from_child_runs(self, task):
        response = self._post({"kind": "repeat", "serial": "S1", "scenario": "cold_start",
                               "duration": 30, "iterations": 2})
        campaign_id = response.json()["campaign_id"]
        runs = self.storage.list_campaign_runs(campaign_id)

        self.assertEqual(self.client.get(f"/api/campaigns/{campaign_id}").json()["status"], "running")
        self.storage.update_run(runs[0]["id"], "completed")
        detail = self.client.get(f"/api/campaigns/{campaign_id}").json()
        self.assertEqual(detail["status"], "running")
        self.assertAlmostEqual(detail["progress_pct"], 50.0)

        self.storage.update_run(runs[1]["id"], "failed")
        detail = self.client.get(f"/api/campaigns/{campaign_id}").json()
        self.assertEqual(detail["status"], "completed")
        self.assertAlmostEqual(detail["progress_pct"], 100.0)

    def test_list_and_detail_agree_on_derived_status(self, task):
        campaign_id = self._post({"kind": "repeat", "serial": "S1", "scenario": "cold_start",
                                  "duration": 30, "iterations": 2}).json()["campaign_id"]
        for run in self.storage.list_campaign_runs(campaign_id):
            self.storage.update_run(run["id"], "completed")

        listed = self.client.get("/api/campaigns").json()[0]
        detail = self.client.get(f"/api/campaigns/{campaign_id}").json()
        self.assertEqual(listed["status"], detail["status"])
        self.assertEqual(listed["finished_count"], detail["finished_count"])

    def test_cancel_flags_unstarted_runs(self, task):
        campaign_id = self._post({"kind": "repeat", "serial": "S1", "scenario": "cold_start",
                                  "duration": 30, "iterations": 3}).json()["campaign_id"]
        runs = self.storage.list_campaign_runs(campaign_id)
        self.storage.update_run(runs[0]["id"], "completed")

        response = self.client.post(f"/api/campaigns/{campaign_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cancelled_runs"], 2)
        self.assertEqual(self.storage.get_run(runs[0]["id"])["cancel_requested"], 0)
        self.assertEqual(self.storage.get_run(runs[1]["id"])["cancel_requested"], 1)
        self.assertEqual(
            self.client.get(f"/api/campaigns/{campaign_id}").json()["status"], "interrupted"
        )

    def test_delete_removes_child_runs_and_their_recordings(self, task):
        campaign_id = self._post({"kind": "soak", "serial": "S1", "scenario": "play_golden",
                                  "duration": 600}).json()["campaign_id"]
        run_id = self.storage.list_campaign_runs(campaign_id)[0]["id"]
        self.recordings_root.mkdir(parents=True, exist_ok=True)
        recording = self.recordings_root / f"{run_id}.mp4"
        recording.write_bytes(b"fake")

        response = self.client.delete(f"/api/campaigns/{campaign_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.storage.get_campaign(campaign_id))
        self.assertIsNone(self.storage.get_run(run_id))
        self.assertFalse(recording.exists())

    def test_unknown_campaign_returns_404(self, task):
        self.assertEqual(self.client.get("/api/campaigns/nope").status_code, 404)
        self.assertEqual(self.client.post("/api/campaigns/nope/cancel").status_code, 404)
        self.assertEqual(self.client.delete("/api/campaigns/nope").status_code, 404)

    def test_campaign_runs_are_ordinary_runs(self, task):
        # Child runs must stay visible to every existing feature rather than
        # becoming a separate kind of object.
        campaign_id = self._post({"kind": "soak", "serial": "S1", "scenario": "play_golden",
                                  "duration": 600}).json()["campaign_id"]
        run_id = self.storage.list_campaign_runs(campaign_id)[0]["id"]
        self.assertEqual(self.client.get(f"/api/runs/{run_id}").status_code, 200)
        self.assertIn(run_id, {run["id"] for run in self.client.get("/api/runs").json()})
