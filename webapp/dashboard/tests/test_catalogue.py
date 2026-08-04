"""What the UI can offer: YouTube scenarios and suites.

Split out of one 1120-line module; the shared setUp lives in support.ApiTestCase.
"""

import json
from unittest.mock import patch

from autoperf.scenarios.youtube import list_scenarios
from dashboard.tasks import run_test_task

from dashboard.tests.support import ApiTestCase


class CatalogueApiTests(ApiTestCase):
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
        expected_count = len(list_scenarios("smoke"))
        self.assertEqual(payload["tier"], "smoke")
        self.assertEqual(payload["count"], expected_count)
        self.assertEqual(len(payload["run_ids"]), expected_count)
        # Every scenario gets a row; the device gets one task at a time. The
        # rest follow as each finishes -- which is also what makes a suite run
        # in tier order rather than in whatever order the retries settled.
        self.assertEqual(mock_task.apply_async.call_count, 1)
        self.assertEqual(
            [self.storage.get_run(rid)["status"] for rid in payload["run_ids"]],
            ["pending"] * expected_count)
