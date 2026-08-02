"""The run's own account of what happened, and how it reaches a client.

A run that could not carry out its scenario still produces a full set of
perfectly real metrics -- of a screen it never reached. The framework has
recorded that distinction in `test_events` for a long time; until these
endpoints existed it was written and never readable again, so every run in the
list looked identical no matter what it had actually done.

These tests pin the three things a caller needs to tell them apart: a verdict
on every row of the list, the event log itself, and the ability to reproduce a
decayed selector on purpose.
"""

import json
from unittest.mock import patch

from autoperf.models import TestEvent
from autoperf.storage import BatchWriter

from dashboard.tests.support import ApiTestCase


class EvidenceApiTests(ApiTestCase):
    def _run_with_events(self, run_id, serial="S1", events=()):
        self.storage.create_run(run_id, serial, "search_and_play")
        writer = BatchWriter(self.storage)
        writer.start()
        for kind, message, details in events:
            writer.put(TestEvent(run_id, kind, message, details=details))
        writer.close()
        self.storage.update_run(run_id, "completed")

    # --- the list has to carry a verdict -------------------------------------

    def test_run_list_marks_a_clean_run_verified(self):
        self._run_with_events("clean", events=[("adapter_action", "tap_element completed", {})])
        row = next(r for r in self.client.get("/api/runs").json() if r["id"] == "clean")
        self.assertTrue(row["quality"]["verified"])
        self.assertEqual(row["quality"]["selector_fallbacks"], 0)

    def test_run_list_marks_a_run_that_measured_the_wrong_screen(self):
        """The whole point: both runs say 'completed'; only `quality` separates them."""
        self._run_with_events("clean", events=[("adapter_action", "tap completed", {})])
        self._run_with_events("bad", events=[
            ("verification_failed", "nothing playing", {"action": "verify_playing"}),
        ])
        rows = {r["id"]: r for r in self.client.get("/api/runs").json()}
        self.assertEqual(rows["clean"]["status"], rows["bad"]["status"])
        self.assertTrue(rows["clean"]["quality"]["verified"])
        self.assertFalse(rows["bad"]["quality"]["verified"])
        self.assertEqual(rows["bad"]["quality"]["verification_failures"], 1)

    def test_run_list_surfaces_fallbacks_without_calling_them_failures(self):
        self._run_with_events("stale", events=[
            ("selector_fallback", "library_tab resolved by coordinates", {"target": "library_tab"}),
        ])
        row = next(r for r in self.client.get("/api/runs").json() if r["id"] == "stale")
        # A fallback worked -- it is a warning about decay, not a failed run.
        self.assertTrue(row["quality"]["verified"])
        self.assertEqual(row["quality"]["selector_fallbacks"], 1)

    def test_run_list_quality_costs_one_query_not_one_per_run(self):
        for index in range(5):
            self._run_with_events(f"run{index}")
        with patch.object(type(self.storage), "run_quality", side_effect=AssertionError(
                "the list must not fall back to the per-run query")):
            response = self.client.get("/api/runs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 5)

    # --- the event log itself ------------------------------------------------

    def test_events_returns_the_log_oldest_first_with_details_decoded(self):
        self._run_with_events("r1", events=[
            ("adapter_action", "launch_app completed", {"action": "launch_app"}),
            ("ui_introspection", "3 UI dump(s) over 8.4s", {"dumps": 3, "dump_seconds": 8.4}),
        ])
        payload = self.client.get("/api/runs/r1/events").json()
        self.assertEqual([e["kind"] for e in payload["events"]],
                         ["adapter_action", "ui_introspection"])
        # Decoded, not a JSON string the client would have to parse again.
        self.assertEqual(payload["events"][1]["details"]["dumps"], 3)
        self.assertFalse(payload["truncated"])

    def test_events_can_be_filtered_to_one_kind(self):
        self._run_with_events("r2", events=[
            ("adapter_action", "tap completed", {}),
            ("selector_fallback", "library_tab by coordinates", {"target": "library_tab"}),
        ])
        payload = self.client.get("/api/runs/r2/events?kinds=selector_fallback").json()
        self.assertEqual([e["kind"] for e in payload["events"]], ["selector_fallback"])

    def test_events_says_so_when_it_truncates(self):
        self._run_with_events("r3", events=[("adapter_action", f"step {i}", {}) for i in range(4)])
        payload = self.client.get("/api/runs/r3/events?limit=2").json()
        self.assertEqual(len(payload["events"]), 2)
        # A cut-off log that looked complete would be the same class of lie
        # this layer exists to prevent.
        self.assertTrue(payload["truncated"])

    def test_events_carries_the_same_verdict_as_the_list(self):
        self._run_with_events("r4", events=[("verification_failed", "no", {})])
        payload = self.client.get("/api/runs/r4/events").json()
        self.assertFalse(payload["quality"]["verified"])

    def test_events_returns_404_for_a_missing_run(self):
        self.assertEqual(self.client.get("/api/runs/nope/events").status_code, 404)

    def test_run_detail_carries_the_verdict_too(self):
        self._run_with_events("r5", events=[("selector_fallback", "x", {})])
        self.assertEqual(self.client.get("/api/runs/r5").json()["quality"]["selector_fallbacks"], 1)

    # --- reproducing a decayed selector on purpose ---------------------------

    def test_selector_targets_lists_what_can_be_blinded(self):
        targets = self.client.get("/api/selector-targets").json()["targets"]
        self.assertIn("library_tab", targets)
        self.assertEqual(targets, sorted(targets))

    @patch("dashboard.views.trigger_run", return_value="rid")
    def test_run_accepts_blind_targets(self, mock_trigger):
        response = self.client.post("/api/runs", content_type="application/json", data=json.dumps(
            {"serial": "S1", "youtube_scenario": "search_and_play", "blind_targets": ["search_icon"]}))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["blind_targets"], ["search_icon"])
        self.assertEqual(mock_trigger.call_args.kwargs["blind_targets"], ["search_icon"])

    def test_run_rejects_an_unknown_blind_target(self):
        """A typo that blinded nothing would demo a clean run while claiming otherwise."""
        response = self.client.post("/api/runs", content_type="application/json", data=json.dumps(
            {"serial": "S1", "youtube_scenario": "search_and_play", "blind_targets": ["nope"]}))
        self.assertEqual(response.status_code, 400)
        self.assertIn("nope", response.json()["error"])

    def test_run_rejects_blind_targets_without_a_scenario(self):
        response = self.client.post("/api/runs", content_type="application/json", data=json.dumps(
            {"serial": "S1", "blind_targets": ["search_icon"]}))
        self.assertEqual(response.status_code, 400)

    def test_run_rejects_a_malformed_blind_targets_value(self):
        response = self.client.post("/api/runs", content_type="application/json", data=json.dumps(
            {"serial": "S1", "youtube_scenario": "cold_start", "blind_targets": "search_icon"}))
        self.assertEqual(response.status_code, 400)
