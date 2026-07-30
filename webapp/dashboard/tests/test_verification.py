"""A run that never reached its target screen must not read as a pass.
"""

import json

from autoperf.models import MetricSample
from autoperf.storage import BatchWriter

from dashboard.tests.support import ApiTestCase


class VerificationVerdictTests(ApiTestCase):
    def _run(self, run_id, *, values=(40, 41, 42), events=(), version=None):
        from autoperf.models import TestEvent

        self.storage.create_run(run_id, "S1", "cold_start")
        writer = BatchWriter(self.storage)
        writer.start()
        for value in values:
            writer.put(MetricSample(run_id, "cpu", "cpu.total", float(value), "%"))
        for kind in events:
            writer.put(TestEvent(run_id, kind, "message"))
        writer.close()
        if version:
            self.storage.set_run_app_version(run_id, version)
        self.storage.update_run(run_id, "completed")
        return run_id

    def test_an_unverified_run_is_its_own_bucket_not_a_pass_or_a_fail(self):
        self._run("baseline")
        self.storage.set_baseline("S1", "baseline")
        self._run("broken", events=("verification_failed",))

        stats = self.client.get("/api/stats").json()
        verdicts = {r["run_id"]: r["verdict"] for r in stats["runs"]}
        self.assertEqual(verdicts["broken"], "unverified")
        self.assertEqual(stats["unverified"], 1)
        # Excluded from the denominator: a pass rate over runs that measured
        # nothing would be a confident number about nothing.
        self.assertEqual(stats["passed"] + stats["failed"], 1)

    def test_a_coordinate_fallback_alone_still_counts_as_a_pass(self):
        self._run("baseline")
        self.storage.set_baseline("S1", "baseline")
        self._run("degraded", events=("selector_fallback",))

        stats = self.client.get("/api/stats").json()
        verdicts = {r["run_id"]: r["verdict"] for r in stats["runs"]}
        self.assertEqual(verdicts["degraded"], "pass")
        self.assertEqual(stats["unverified"], 0)

    def test_per_scenario_breakdown_carries_the_unverified_count(self):
        self._run("baseline")
        self.storage.set_baseline("S1", "baseline")
        self._run("broken", events=("verification_failed",))

        entry = self.client.get("/api/stats").json()["by_scenario"][0]
        self.assertEqual(entry["unverified"], 1)

    def test_comparison_reports_quality_and_app_version(self):
        self._run("baseline", version={"package": "com.google.android.youtube",
                                       "version_name": "19.09.37", "version_code": 1543})
        self.storage.set_baseline("S1", "baseline")
        self._run("candidate", events=("verification_failed",),
                  version={"package": "com.google.android.youtube",
                           "version_name": "19.16.39", "version_code": 1560})

        comparison = self.client.get("/api/runs/candidate/comparison").json()
        # Both travel with the comparison rather than being separate lookups
        # a caller might skip -- each changes what the delta means.
        self.assertIs(comparison["app_version"]["changed"], True)
        self.assertEqual(comparison["app_version"]["baseline"]["version_name"], "19.09.37")
        self.assertFalse(comparison["candidate_quality"]["verified"])

    def test_comparison_reports_an_unchanged_app_version(self):
        version = {"package": "com.google.android.youtube",
                   "version_name": "19.09.37", "version_code": 1543}
        self._run("baseline", version=version)
        self.storage.set_baseline("S1", "baseline")
        self._run("candidate", version=version)

        comparison = self.client.get("/api/runs/candidate/comparison").json()
        self.assertIs(comparison["app_version"]["changed"], False)
        self.assertTrue(comparison["candidate_quality"]["verified"])

    def test_missing_versions_are_unknown_rather_than_unchanged(self):
        self._run("baseline")
        self.storage.set_baseline("S1", "baseline")
        self._run("candidate")

        comparison = self.client.get("/api/runs/candidate/comparison").json()
        self.assertIsNone(comparison["app_version"]["changed"])
