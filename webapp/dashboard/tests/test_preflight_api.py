"""Preflight from the dashboard: enqueue, refuse, report, and the device lock.

Preflight already existed as a CLI command. What is new here is that it can be
asked for from the app -- which means it now competes with runs for a device,
and its verdict has to survive somewhere a page can read it.
"""

import json
from unittest.mock import patch

from autoperf import preflight
from dashboard.services import trigger_preflight

from dashboard.tests.support import ApiTestCase


class PreflightEnqueueTests(ApiTestCase):
    @patch("dashboard.services.run_preflight_task")
    def test_trigger_creates_pending_row_and_enqueues(self, mock_task):
        preflight_id = trigger_preflight(self.storage, "S1", "search_and_play")
        row = self.storage.get_preflight(preflight_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["device_serial"], "S1")
        self.assertEqual(row["scenario"], "search_and_play")
        mock_task.apply_async.assert_called_once_with(
            args=[self.storage.path, "S1", preflight_id, "search_and_play"],
            task_id=preflight_id,
        )

    @patch("dashboard.services.run_preflight_task")
    def test_post_scopes_the_check_to_one_scenario(self, mock_task):
        response = self.client.post(
            "/api/preflights",
            data=json.dumps({"serial": "S1", "youtube_scenario": "search_and_play"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["scenarios"], ["search_and_play"])
        # The caller is told what will be graded before the device is touched.
        self.assertEqual(payload["targets"], sorted(preflight.targets_of("search_and_play")))
        self.assertTrue(payload["targets"])

    @patch("dashboard.services.run_preflight_task")
    def test_post_without_a_scenario_uses_the_covering_set(self, mock_task):
        response = self.client.post(
            "/api/preflights",
            data=json.dumps({"serial": "S1"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["scenarios"], preflight.covering_scenarios())

    @patch("dashboard.services.run_preflight_task")
    def test_deep_link_preset_is_refused_rather_than_run_green(self, mock_task):
        # play_golden resolves no selector at all: preflighting it would report
        # success having checked nothing, which is the failure mode the whole
        # verification layer exists to prevent.
        self.assertEqual(preflight.targets_of("play_golden"), set())
        response = self.client.post(
            "/api/preflights",
            data=json.dumps({"serial": "S1", "youtube_scenario": "play_golden"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("no selectors", response.json()["error"])
        mock_task.apply_async.assert_not_called()

    @patch("dashboard.services.run_preflight_task")
    def test_unknown_scenario_is_rejected(self, mock_task):
        response = self.client.post(
            "/api/preflights",
            data=json.dumps({"serial": "S1", "youtube_scenario": "nope"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        mock_task.apply_async.assert_not_called()

    def test_serial_is_required(self):
        response = self.client.post("/api/preflights", data=json.dumps({}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)


class PreflightReportTests(ApiTestCase):
    SUMMARY = {
        "ok": False,
        "scenarios_checked": ["search_and_play"],
        "targets_checked": 3,
        "targets_ok": 2,
        "targets_needing_attention": 1,
        "needs_attention": [{"target": "search_bar", "status": ["coordinates"],
                             "observed_on_screen": [{"text": "搜尋"}]}],
        "healthy": [], "not_applicable": [], "failed_verifications": [],
    }

    def test_report_round_trips_and_lifts_ok_out(self):
        self.storage.create_preflight("p1", "S1", "search_and_play")
        self.storage.finish_preflight("p1", "completed", summary=self.SUMMARY,
                                      app_version={"package": "com.google.android.youtube",
                                                   "version_name": "21.29.366",
                                                   "version_code": 1})
        response = self.client.get("/api/preflights/p1")
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertIs(report["ok"], False)
        self.assertEqual(report["app_version_name"], "21.29.366")
        self.assertEqual(report["summary"]["needs_attention"][0]["target"], "search_bar")

    def test_list_omits_the_summary_but_keeps_the_verdict(self):
        self.storage.create_preflight("p1", "S1", "search_and_play")
        self.storage.finish_preflight("p1", "completed", summary=self.SUMMARY)
        rows = self.client.get("/api/preflights").json()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("summary", rows[0])
        self.assertIs(rows[0]["ok"], False)

    def test_list_filters_by_device(self):
        self.storage.create_preflight("p1", "S1")
        self.storage.create_preflight("p2", "S2")
        rows = self.client.get("/api/preflights?device=S2").json()
        self.assertEqual([row["id"] for row in rows], ["p2"])

    def test_missing_report_is_a_404(self):
        self.assertEqual(self.client.get("/api/preflights/nope").status_code, 404)

    def test_targets_for_one_scenario_are_readable_before_committing_a_device(self):
        response = self.client.get("/api/selector-targets?scenario=search_and_play")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["targets"],
                         sorted(preflight.targets_of("search_and_play")))

    def test_targets_of_a_deep_link_preset_is_an_empty_list_not_an_error(self):
        response = self.client.get("/api/selector-targets?scenario=play_golden")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["targets"], [])


class PreflightDeviceLockTests(ApiTestCase):
    """One device, one driver. Preflight taps and dumps like a run does."""

    def test_preflight_cannot_start_while_a_run_holds_the_device(self):
        self.storage.create_run("run1", "S1")
        self.assertTrue(self.storage.try_start_run("run1"))
        self.storage.create_preflight("p1", "S1")
        self.assertFalse(self.storage.try_start_preflight("p1"))

    def test_run_cannot_start_while_a_preflight_holds_the_device(self):
        self.storage.create_preflight("p1", "S1")
        self.assertTrue(self.storage.try_start_preflight("p1"))
        self.storage.create_run("run1", "S1")
        self.assertFalse(self.storage.try_start_run("run1"))

    def test_a_finished_preflight_releases_the_device(self):
        self.storage.create_preflight("p1", "S1")
        self.assertTrue(self.storage.try_start_preflight("p1"))
        self.storage.finish_preflight("p1", "completed", summary={"ok": True})
        self.storage.create_run("run1", "S1")
        self.assertTrue(self.storage.try_start_run("run1"))

    def test_a_failed_preflight_releases_the_device_too(self):
        # The one that matters: a preflight that raised must not leave the
        # serial locked against every later run.
        self.storage.create_preflight("p1", "S1")
        self.assertTrue(self.storage.try_start_preflight("p1"))
        self.storage.finish_preflight("p1", "failed", error="adb died")
        self.storage.create_run("run1", "S1")
        self.assertTrue(self.storage.try_start_run("run1"))

    def test_another_device_is_unaffected(self):
        self.storage.create_preflight("p1", "S1")
        self.assertTrue(self.storage.try_start_preflight("p1"))
        self.storage.create_run("run2", "S2")
        self.assertTrue(self.storage.try_start_run("run2"))

    def test_two_preflights_on_one_device_serialize(self):
        self.storage.create_preflight("p1", "S1")
        self.storage.create_preflight("p2", "S1")
        self.assertTrue(self.storage.try_start_preflight("p1"))
        self.assertFalse(self.storage.try_start_preflight("p2"))


class PreflightTaskTests(ApiTestCase):
    """The task body, with the device replaced."""

    def _patches(self, summary):
        return (
            patch("dashboard.tasks.AdbClient"),
            patch("dashboard.tasks.select_profile"),
            patch("dashboard.tasks.preflight.run_preflight", return_value=summary),
            patch("dashboard.tasks.uiauto.package_version",
                  return_value={"package": "com.google.android.youtube",
                                "version_name": "21.29.366", "version_code": 2}),
        )

    def test_task_stores_the_summary_and_the_app_build(self):
        from dashboard.tasks import run_preflight_task

        summary = {"ok": True, "targets_checked": 3, "needs_attention": []}
        adb, profile, run, version = self._patches(summary)
        with adb, profile, run as mock_run, version:
            run_preflight_task(self.storage.path, "S1", "p1", "search_and_play")
            # Scoped: the covering set is not what was asked for.
            self.assertEqual(mock_run.call_args.kwargs["scenarios"], ["search_and_play"])

        report = self.storage.get_preflight("p1")
        self.assertEqual(report["status"], "completed")
        self.assertIs(report["ok"], True)
        self.assertEqual(report["app_version_name"], "21.29.366")

    def test_a_raising_preflight_is_recorded_as_failed(self):
        from dashboard.tasks import run_preflight_task

        adb, profile, _, version = self._patches(None)
        with adb, profile, version, \
                patch("dashboard.tasks.preflight.run_preflight",
                      side_effect=RuntimeError("device went away")):
            with self.assertRaises(RuntimeError):
                run_preflight_task(self.storage.path, "S1", "p1", "search_and_play")

        report = self.storage.get_preflight("p1")
        self.assertEqual(report["status"], "failed")
        self.assertIn("device went away", report["error"])
        self.assertIsNone(report["ok"])

    def test_cancel_asks_a_running_preflight_to_stop(self):
        self.storage.create_preflight("pf1", "S1", "cold_start")
        self.storage.try_start_preflight("pf1")

        response = self.client.post("/api/preflights/pf1/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.storage.preflight_cancel_requested("pf1"))

    def test_cancel_is_404_for_an_unknown_preflight(self):
        self.assertEqual(self.client.post("/api/preflights/nope/cancel").status_code, 404)

    def test_cancel_is_rejected_once_it_has_finished(self):
        # Nothing to stop, and saying "cancelled" would imply it was.
        self.storage.create_preflight("pf1", "S1", "cold_start")
        self.storage.finish_preflight("pf1", "completed", summary={"ok": True})

        response = self.client.post("/api/preflights/pf1/cancel")

        self.assertEqual(response.status_code, 400)
