import tempfile
import unittest
from pathlib import Path

from autoperf import campaigns
from autoperf.adapters import AndroidAdapter
from autoperf.campaigns import CampaignSpec
from autoperf.models import MetricSample, RunStatus
from autoperf.scenarios.youtube import list_scenarios
from autoperf.storage import BatchWriter, Storage
from tests.support import DeviceAdb


def FakeAdb():
    """A phone that answers everything a scenario-driven run asks it."""
    return DeviceAdb(metrics=True)


def _storage(directory):
    storage = Storage(Path(directory) / "campaigns.db")
    storage.initialize()
    return storage


class CampaignSpecTests(unittest.TestCase):
    def test_soak_forces_a_single_iteration(self):
        spec = CampaignSpec("soak", "S1", 3600, scenario="play_golden", iterations=50).validated()
        self.assertEqual(spec.iterations, 1)
        self.assertEqual(spec.planned_scenarios(), ["play_golden"])

    def test_soak_without_a_scenario_is_a_plain_sampling_run(self):
        spec = CampaignSpec("soak", "S1", 3600).validated()
        self.assertEqual(spec.planned_scenarios(), [None])

    def test_tier_repeat_is_ordered_iteration_major(self):
        # A campaign cut short must leave an even number of samples per
        # scenario, not all of scenario A and none of the rest.
        spec = CampaignSpec("repeat", "S1", 30, tier="smoke", iterations=3).validated()
        tier = list_scenarios(tier="smoke")
        self.assertEqual(spec.planned_scenarios(), tier * 3)

    def test_scenario_repeat_plans_one_run_per_iteration(self):
        spec = CampaignSpec("repeat", "S1", 30, scenario="cold_start", iterations=4).validated()
        self.assertEqual(spec.planned_scenarios(), ["cold_start"] * 4)

    def test_rejects_invalid_specs(self):
        cases = [
            (CampaignSpec("nope", "S1", 30), "kind must be one of"),
            (CampaignSpec("soak", "", 30), "serial is required"),
            (CampaignSpec("soak", "S1", 0), "duration must be positive"),
            (CampaignSpec("soak", "S1", -1), "duration must be positive"),
            (CampaignSpec("repeat", "S1", 30), "needs either a scenario or a tier"),
            (CampaignSpec("repeat", "S1", 30, scenario="x", iterations=0), "at least 1"),
            (CampaignSpec("repeat", "S1", 30, tier="nope", iterations=1), "tier must be one of"),
            (CampaignSpec("repeat", "S1", 30, tier="regression", iterations=999), "above the limit"),
        ]
        for spec, expected in cases:
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError) as ctx:
                    spec.validated()
                self.assertIn(expected, str(ctx.exception))

    def test_validation_does_not_mutate_the_original_spec(self):
        original = CampaignSpec("soak", "S1", 3600, iterations=9)
        original.validated()
        self.assertEqual(original.iterations, 9)


class CreateCampaignTests(unittest.TestCase):
    def test_creates_every_child_run_up_front(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 30, tier="smoke", iterations=2)
            )
            runs = storage.list_campaign_runs(created["campaign_id"])

            self.assertEqual(created["count"], len(list_scenarios(tier="smoke")) * 2)
            self.assertEqual(len(runs), created["count"])
            # All pending: the campaign's full extent is visible immediately,
            # so progress is finished/total rather than a growing total.
            self.assertTrue(all(run["status"] == "pending" for run in runs))
            self.assertEqual([run["id"] for run in runs], created["run_ids"])

    def test_rejects_an_invalid_spec_without_writing_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            with self.assertRaises(ValueError):
                campaigns.create_campaign(storage, CampaignSpec("repeat", "S1", 30))
            self.assertEqual(storage.list_campaigns(), [])


class ExecuteCampaignTests(unittest.TestCase):
    def test_runs_every_child_run_and_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 0.2, scenario="cold_start", iterations=3)
            )
            seen = []
            result = campaigns.execute_campaign(
                storage, FakeAdb(), created["campaign_id"],
                adapter_factory=lambda adb, serial: AndroidAdapter(),
                on_run=seen.append,
            )

            self.assertEqual(result["executed"], 3)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(seen), 3)
            self.assertTrue(all(run["status"] == "completed"
                                for run in storage.list_campaign_runs(created["campaign_id"])))

    def test_skips_already_finished_runs_so_it_doubles_as_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 0.2, scenario="cold_start", iterations=3)
            )
            storage.update_run(created["run_ids"][0], RunStatus.COMPLETED)

            result = campaigns.execute_campaign(
                storage, FakeAdb(), created["campaign_id"],
                adapter_factory=lambda adb, serial: AndroidAdapter(),
            )
            self.assertEqual(result["executed"], 2)

    def test_a_failing_run_does_not_abandon_the_rest_of_the_campaign(self):
        # A campaign exists to gather many samples; one device hiccup is
        # data, not a reason to stop collecting.
        half_broken = DeviceAdb(metrics=True, fail_first={"wm size": 1})
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 0.2, scenario="cold_start", iterations=3)
            )
            result = campaigns.execute_campaign(
                storage, half_broken, created["campaign_id"],
                adapter_factory=lambda adb, serial: AndroidAdapter(),
            )

            statuses = [run["status"] for run in storage.list_campaign_runs(created["campaign_id"])]
            self.assertEqual(result["executed"], 3)
            self.assertEqual(statuses[0], "failed")
            self.assertEqual(statuses[1:], ["completed", "completed"])

    def test_stops_when_the_campaign_is_cancelled_midway(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 0.2, scenario="cold_start", iterations=4)
            )

            def cancel_after_first(run):
                storage.request_campaign_cancel(created["campaign_id"])

            result = campaigns.execute_campaign(
                storage, FakeAdb(), created["campaign_id"],
                adapter_factory=lambda adb, serial: AndroidAdapter(),
                on_run=cancel_after_first,
            )
            self.assertEqual(result["executed"], 1)
            self.assertEqual(result["status"], "interrupted")

    def test_seeds_a_baseline_from_the_first_successful_run(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 0.2, scenario="cold_start", iterations=2)
            )
            campaigns.execute_campaign(
                storage, FakeAdb(), created["campaign_id"],
                adapter_factory=lambda adb, serial: AndroidAdapter(),
            )
            baseline = storage.get_baseline("S1", "cold_start")
            self.assertIsNotNone(baseline)
            self.assertEqual(baseline["run_id"], created["run_ids"][0])

    def test_unknown_campaign_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            with self.assertRaises(ValueError):
                campaigns.execute_campaign(storage, FakeAdb(), "missing")


class CampaignAnalysisTests(unittest.TestCase):
    def _write(self, storage, run_id, name, values, unit="%"):
        writer = BatchWriter(storage)
        writer.start()
        for value in values:
            writer.put(MetricSample(run_id, "collector", name, float(value), unit))
        writer.close()

    def test_flags_a_scenario_that_passes_and_fails_the_same_test(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 30, scenario="cold_start", iterations=3)
            )
            for index, run_id in enumerate(created["run_ids"]):
                storage.update_run(run_id, RunStatus.FAILED if index == 0 else RunStatus.COMPLETED)
                if index:
                    self._write(storage, run_id, "cpu.total", [40, 41, 42])

            detail = campaigns.campaign_detail(storage, created["campaign_id"])
            entry = detail["repeat"]["scenarios"][0]
            self.assertTrue(entry["flaky"])
            self.assertEqual(entry["errored"], 1)
            self.assertEqual(entry["completed"], 2)
            self.assertAlmostEqual(entry["pass_rate"], 200 / 3, places=6)

    def test_consistent_runs_are_not_flaky(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 30, scenario="cold_start", iterations=3)
            )
            for run_id in created["run_ids"]:
                storage.update_run(run_id, RunStatus.COMPLETED)
                self._write(storage, run_id, "cpu.total", [40, 41, 42])

            entry = campaigns.campaign_detail(
                storage, created["campaign_id"])["repeat"]["scenarios"][0]
            self.assertFalse(entry["flaky"])
            self.assertEqual(entry["pass_rate"], 100.0)

    def test_reports_metric_spread_across_iterations(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 30, scenario="cold_start", iterations=2)
            )
            storage.update_run(created["run_ids"][0], RunStatus.COMPLETED)
            self._write(storage, created["run_ids"][0], "cpu.total", [10, 10, 10])
            storage.update_run(created["run_ids"][1], RunStatus.COMPLETED)
            self._write(storage, created["run_ids"][1], "cpu.total", [30, 30, 30])

            metric = campaigns.campaign_detail(
                storage, created["campaign_id"])["repeat"]["scenarios"][0]["metric_stability"][0]
            self.assertAlmostEqual(metric["mean"], 20.0, places=6)
            self.assertAlmostEqual(metric["cv_pct"], 50.0, places=6)

    def test_soak_detail_reports_drift(self):
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("soak", "S1", 28800, scenario="play_golden")
            )
            run_id = created["run_ids"][0]
            origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
            writer = BatchWriter(storage)
            writer.start()
            for i in range(200):
                writer.put(MetricSample(
                    run_id, "memory", "memory.used", 2_000_000 + 600_000 * i / 199, "KiB",
                    timestamp=(origin + timedelta(seconds=i * 144)).isoformat(),
                ))
            writer.close()
            storage.update_run(run_id, RunStatus.COMPLETED)

            soak = campaigns.campaign_detail(storage, created["campaign_id"])["soak"]
            self.assertEqual(soak["run_id"], run_id)
            self.assertGreater(soak["trends"][0]["drift_pct"], 20)
            self.assertGreater(soak["trends"][0]["slope_per_hour"], 0)

    def test_status_is_derived_from_child_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 30, scenario="cold_start", iterations=2)
            )
            campaign_id = created["campaign_id"]
            self.assertEqual(campaigns.campaign_detail(storage, campaign_id)["status"], "running")

            storage.update_run(created["run_ids"][0], RunStatus.COMPLETED)
            detail = campaigns.campaign_detail(storage, campaign_id)
            self.assertEqual(detail["status"], "running")
            self.assertAlmostEqual(detail["progress_pct"], 50.0)

            storage.update_run(created["run_ids"][1], RunStatus.FAILED)
            detail = campaigns.campaign_detail(storage, campaign_id)
            self.assertEqual(detail["status"], "completed")
            self.assertAlmostEqual(detail["progress_pct"], 100.0)

    def test_summaries_agree_with_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 30, scenario="cold_start", iterations=2)
            )
            for run_id in created["run_ids"]:
                storage.update_run(run_id, RunStatus.COMPLETED)

            summary = campaigns.campaign_summaries(storage)[0]
            detail = campaigns.campaign_detail(storage, created["campaign_id"])
            self.assertEqual(summary["status"], detail["status"])
            self.assertEqual(summary["finished_count"], detail["finished_count"])

    def test_cancel_flags_only_unfinished_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            created = campaigns.create_campaign(
                storage, CampaignSpec("repeat", "S1", 30, scenario="cold_start", iterations=3)
            )
            storage.update_run(created["run_ids"][0], RunStatus.COMPLETED)

            result = campaigns.cancel_campaign(storage, created["campaign_id"])
            self.assertEqual(result["cancelled_runs"], 2)
            self.assertEqual(
                campaigns.campaign_detail(storage, created["campaign_id"])["status"], "interrupted"
            )

    def test_cancel_unknown_campaign_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(directory)
            with self.assertRaises(ValueError):
                campaigns.cancel_campaign(storage, "missing")


if __name__ == "__main__":
    unittest.main()
