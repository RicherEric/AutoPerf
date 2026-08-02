import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from autoperf.models import Device, MetricSample, RunOrigin, RunStatus, TestEvent
from autoperf.storage import BatchWriter, Storage


class StorageTests(unittest.TestCase):
    def test_initialize_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.initialize()

    def test_initialize_migrates_databases_predating_youtube_scenario_column(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "db.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE test_runs (id TEXT PRIMARY KEY, device_serial TEXT NOT NULL, status TEXT NOT NULL,"
                " started_at TEXT, finished_at TEXT, checkpoint TEXT, error TEXT)"
            )
            conn.commit()
            conn.close()

            storage = Storage(db_path)
            storage.initialize()  # must not raise, and must add the missing column
            storage.create_run("run1", "device", youtube_scenario="cold_start")
            self.assertEqual(storage.get_run("run1")["youtube_scenario"], "cold_start")

    def test_create_run_records_youtube_scenario(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device", youtube_scenario="home_feed_scroll")
            self.assertEqual(storage.get_run("run1")["youtube_scenario"], "home_feed_scroll")

    def test_create_run_defaults_youtube_scenario_to_none(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            self.assertIsNone(storage.get_run("run1")["youtube_scenario"])

    def test_register_device_upserts_on_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.register_device(Device("S1", "device", "Pixel", "pixel"))
            storage.register_device(Device("S1", "device", "Pixel 2", "pixel2"))
            conn = storage.connect()
            try:
                row = conn.execute("SELECT model, product FROM devices WHERE serial=?", ("S1",)).fetchone()
            finally:
                conn.close()
            self.assertEqual(row, ("Pixel 2", "pixel2"))

    def test_get_run_returns_none_for_missing_run(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            self.assertIsNone(storage.get_run("missing"))

    def test_update_run_sets_started_at_once_and_finished_at_on_terminal_status(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            storage.update_run("run1", RunStatus.RUNNING)
            first_started = storage.get_run("run1")["started_at"]
            self.assertIsNone(storage.get_run("run1")["finished_at"])

            time.sleep(0.01)
            storage.update_run("run1", RunStatus.RUNNING)
            self.assertEqual(storage.get_run("run1")["started_at"], first_started)

            storage.update_run("run1", RunStatus.COMPLETED)
            run = storage.get_run("run1")
            self.assertEqual(run["status"], "completed")
            self.assertIsNotNone(run["finished_at"])

    def test_try_start_run_succeeds_when_device_is_free(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            self.assertTrue(storage.try_start_run("run1"))
            self.assertEqual(storage.get_run("run1")["status"], "running")

    def test_try_start_run_fails_while_another_run_on_same_device_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            storage.create_run("run2", "device")
            self.assertTrue(storage.try_start_run("run1"))
            self.assertFalse(storage.try_start_run("run2"))
            self.assertEqual(storage.get_run("run2")["status"], "pending")

    def test_try_start_run_succeeds_once_the_other_run_is_no_longer_running(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            storage.create_run("run2", "device")
            self.assertTrue(storage.try_start_run("run1"))
            self.assertFalse(storage.try_start_run("run2"))
            storage.update_run("run1", RunStatus.COMPLETED)
            self.assertTrue(storage.try_start_run("run2"))

    def test_try_start_run_does_not_block_different_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "deviceA")
            storage.create_run("run2", "deviceB")
            self.assertTrue(storage.try_start_run("run1"))
            self.assertTrue(storage.try_start_run("run2"))

    def test_try_start_run_allows_resuming_the_same_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            self.assertTrue(storage.try_start_run("run1"))
            storage.update_run("run1", RunStatus.INTERRUPTED)
            self.assertTrue(storage.try_start_run("run1"))

    def test_batch_writer_flushes_all_items_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            writer = BatchWriter(storage, batch_size=3)
            writer.start()
            for _ in range(7):
                writer.put(TestEvent("run1", "kind", "message"))
            writer.close()
            conn = storage.connect()
            try:
                count = conn.execute("SELECT count(*) FROM test_events WHERE run_id=?", ("run1",)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 7)

    def test_put_raises_when_queue_is_full(self):
        storage = Storage("unused.db")
        writer = BatchWriter(storage, queue_size=1, put_timeout=0.01)
        writer.put(TestEvent("run1", "kind", "message"))
        with self.assertRaises(RuntimeError):
            writer.put(TestEvent("run1", "kind", "message2"))

    def test_list_devices_returns_registered_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.register_device(Device("S1", "device", "Pixel", "pixel"))
            storage.register_device(Device("S2", "device", "Galaxy", "galaxy"))
            serials = {row["serial"] for row in storage.list_devices()}
            self.assertEqual(serials, {"S1", "S2"})

    def test_register_device_merges_extra_info_into_flat_dict(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.register_device(
                Device("S1", "device", "Pixel", "pixel"),
                extra_info={"manufacturer": "Google", "sdk_version": "34"},
            )
            device = storage.list_devices()[0]
            self.assertEqual(device["manufacturer"], "Google")
            self.assertEqual(device["sdk_version"], "34")
            self.assertNotIn("extra_info", device)

    def test_register_device_without_extra_info_still_lists_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.register_device(Device("S1", "device", "Pixel", "pixel"))
            device = storage.list_devices()[0]
            self.assertNotIn("extra_info", device)

    def test_list_runs_orders_most_recent_first(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            storage.create_run("run2", "device")
            runs = storage.list_runs()
            self.assertEqual([r["id"] for r in runs], ["run2", "run1"])

    def test_list_runs_respects_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            storage.create_run("run2", "device")
            self.assertEqual(len(storage.list_runs(limit=1)), 1)

    def test_list_runs_filters_by_device_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "S1")
            storage.create_run("run2", "S2")
            runs = storage.list_runs(device_serial="S1")
            self.assertEqual([r["id"] for r in runs], ["run1"])

    def test_list_running_runs_returns_only_running_status(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            storage.create_run("run2", "device")
            storage.update_run("run1", RunStatus.RUNNING)
            storage.update_run("run2", RunStatus.COMPLETED)
            running = storage.list_running_runs()
            self.assertEqual([r["id"] for r in running], ["run1"])

    def test_list_samples_filters_by_since_id_and_orders_by_id(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            writer = BatchWriter(storage)
            writer.start()
            for value in (1.0, 2.0, 3.0):
                writer.put(MetricSample("run1", "cpu", "cpu.total", value, "%"))
            writer.close()

            all_samples = storage.list_samples("run1")
            self.assertEqual([s["value"] for s in all_samples], [1.0, 2.0, 3.0])

            first_id = all_samples[0]["id"]
            remaining = storage.list_samples("run1", since_id=first_id)
            self.assertEqual([s["value"] for s in remaining], [2.0, 3.0])

    def test_delete_run_removes_run_samples_and_events(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            writer = BatchWriter(storage)
            writer.start()
            writer.put(MetricSample("run1", "cpu", "cpu.total", 1.0, "%"))
            writer.put(TestEvent("run1", "lifecycle", "run started"))
            writer.close()

            storage.delete_run("run1")

            self.assertIsNone(storage.get_run("run1"))
            self.assertEqual(storage.list_samples("run1"), [])
            conn = storage.connect()
            try:
                count = conn.execute("SELECT count(*) FROM test_events WHERE run_id=?", ("run1",)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_delete_run_is_a_noop_for_missing_run(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.delete_run("does-not-exist")  # must not raise

    def test_get_baseline_returns_none_when_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            self.assertIsNone(storage.get_baseline("S1"))

    def test_set_baseline_then_get_returns_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.set_baseline("S1", "run1")
            self.assertEqual(storage.get_baseline("S1")["run_id"], "run1")

    def test_set_baseline_overwrites_previous_baseline_for_same_device(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.set_baseline("S1", "run1")
            storage.set_baseline("S1", "run2")
            self.assertEqual(storage.get_baseline("S1")["run_id"], "run2")

    def test_baselines_are_scoped_per_scenario_not_shared_across_them(self):
        # A device's baseline is scoped to a specific scenario -- setting a
        # baseline for "cold_start" must not answer a lookup for a
        # completely different scenario like "multi_video_session", since a
        # heavier scenario naturally uses more resources with no real
        # regression involved.
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "S1", youtube_scenario="cold_start")
            storage.set_baseline("S1", "run1")

            self.assertEqual(storage.get_baseline("S1", "cold_start")["run_id"], "run1")
            self.assertIsNone(storage.get_baseline("S1", "multi_video_session"))
            self.assertIsNone(storage.get_baseline("S1"))  # the plain/no-scenario baseline

    def test_baseline_scenario_is_derived_from_the_run_not_the_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "S1", youtube_scenario="like_video")
            storage.set_baseline("S1", "run1")
            self.assertEqual(storage.get_baseline("S1", "like_video")["run_id"], "run1")


def _storage_with_samples(directory, run_id="run1", samples=()):
    storage = Storage(Path(directory) / "db.sqlite")
    storage.initialize()
    storage.create_run(run_id, "device")
    writer = BatchWriter(storage)
    writer.start()
    for sample in samples:
        writer.put(sample)
    writer.close()
    return storage


class AggregateSamplesTests(unittest.TestCase):
    def test_matches_compute_stats_on_the_same_data(self):
        from autoperf.analyzer import compute_stats, stats_from_aggregates

        samples = [
            MetricSample("run1", "cpu", "cpu.total", float(value), "%")
            for value in (10, 20, 30, 40, 55)
        ] + [
            MetricSample("run1", "memory", "memory.used", float(value), "KiB")
            for value in (2_000_000, 2_400_000, 2_100_000)
        ]
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage_with_samples(directory, samples=samples)
            aggregated = stats_from_aggregates(storage.aggregate_samples("run1"))
            direct = compute_stats(storage.list_samples("run1", limit=100_000))

        self.assertEqual(set(aggregated), set(direct))
        for name, stat in direct.items():
            self.assertEqual(aggregated[name].count, stat.count)
            self.assertAlmostEqual(aggregated[name].mean, stat.mean, places=6)
            self.assertAlmostEqual(aggregated[name].stdev, stat.stdev, places=6)
            self.assertAlmostEqual(aggregated[name].minimum, stat.minimum, places=6)
            self.assertAlmostEqual(aggregated[name].maximum, stat.maximum, places=6)

    def test_sees_samples_beyond_the_old_100k_row_limit(self):
        """The regression this method exists to prevent.

        `compute_stats(list_samples(limit=100_000))` orders by id ASC, so on a
        run longer than ~8 hours it silently dropped the tail -- the part of a
        soak run where a leak actually shows. Here the tail is deliberately
        the only place the maximum lives: a truncating implementation reports
        the wrong maximum and a mean pulled toward the early values.
        """
        from autoperf.analyzer import compute_stats, stats_from_aggregates

        low = [MetricSample("run1", "cpu", "cpu.total", 10.0, "%") for _ in range(120)]
        high = [MetricSample("run1", "cpu", "cpu.total", 90.0, "%") for _ in range(30)]
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage_with_samples(directory, samples=low + high)
            aggregated = stats_from_aggregates(storage.aggregate_samples("run1"))
            truncated = compute_stats(storage.list_samples("run1", limit=120))

        self.assertEqual(aggregated["cpu.total"].count, 150)
        self.assertEqual(aggregated["cpu.total"].maximum, 90.0)
        # The truncated view sees only the healthy opening stretch.
        self.assertEqual(truncated["cpu.total"].count, 120)
        self.assertEqual(truncated["cpu.total"].maximum, 10.0)

    def test_single_sample_metric_reports_zero_stdev(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage_with_samples(
                directory, samples=[MetricSample("run1", "battery", "battery.level", 80.0, "%")]
            )
            rows = {row["name"]: row for row in storage.aggregate_samples("run1")}
        from autoperf.analyzer import stats_from_aggregates

        stats = stats_from_aggregates(list(rows.values()))
        self.assertEqual(stats["battery.level"].count, 1)
        self.assertEqual(stats["battery.level"].stdev, 0.0)

    def test_empty_run_aggregates_to_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage_with_samples(directory)
            self.assertEqual(storage.aggregate_samples("run1"), [])


class DownsampleSamplesTests(unittest.TestCase):
    def test_caps_points_per_metric_independently(self):
        # cpu samples 10x more often than battery; each must still get the
        # full bucket budget rather than battery being crowded out.
        samples = [MetricSample("run1", "cpu", "cpu.total", float(i), "%") for i in range(500)]
        samples += [MetricSample("run1", "battery", "battery.level", float(i), "%") for i in range(50)]
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage_with_samples(directory, samples=samples)
            rows = storage.downsample_samples("run1", buckets=10)

        counts = {}
        for row in rows:
            counts[row["name"]] = counts.get(row["name"], 0) + 1
        self.assertEqual(counts, {"cpu.total": 10, "battery.level": 10})

    def test_preserves_extremes_inside_a_bucket(self):
        # A single spike must survive bucketing -- averaging it away would
        # defeat the point of watching a long run.
        values = [10.0] * 99 + [500.0]
        samples = [MetricSample("run1", "cpu", "cpu.total", v, "%") for v in values]
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage_with_samples(directory, samples=samples)
            rows = storage.downsample_samples("run1", buckets=5)
        self.assertEqual(max(row["maximum"] for row in rows), 500.0)

    def test_fewer_samples_than_buckets_yields_one_point_each(self):
        samples = [MetricSample("run1", "cpu", "cpu.total", float(i), "%") for i in range(3)]
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage_with_samples(directory, samples=samples)
            rows = storage.downsample_samples("run1", buckets=50)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["count"] for row in rows], [1, 1, 1])


class RunQualityTests(unittest.TestCase):
    def _storage_with_events(self, directory, kinds):
        storage = Storage(Path(directory) / "db.sqlite")
        storage.initialize()
        storage.create_run("run1", "device")
        writer = BatchWriter(storage)
        writer.start()
        for kind in kinds:
            writer.put(TestEvent("run1", kind, "message"))
        writer.close()
        return storage

    def test_a_clean_run_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = self._storage_with_events(directory, ["lifecycle", "adapter_action"])
            quality = storage.run_quality("run1")
            self.assertTrue(quality["verified"])
            self.assertEqual(quality["verification_failures"], 0)

    def test_a_verification_failure_makes_the_run_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = self._storage_with_events(
                directory, ["adapter_action", "verification_failed", "verification_failed"])
            quality = storage.run_quality("run1")
            self.assertFalse(quality["verified"])
            self.assertEqual(quality["verification_failures"], 2)

    def test_a_coordinate_fallback_is_counted_but_does_not_unverify(self):
        # The step worked -- exactly as well as before selectors existed --
        # so it is a warning about decay, not a failure.
        with tempfile.TemporaryDirectory() as directory:
            storage = self._storage_with_events(directory, ["selector_fallback", "adapter_action"])
            quality = storage.run_quality("run1")
            self.assertTrue(quality["verified"])
            self.assertEqual(quality["selector_fallbacks"], 1)

    def test_counts_are_scoped_to_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = self._storage_with_events(directory, ["verification_failed"])
            storage.create_run("run2", "device")
            self.assertTrue(storage.run_quality("run2")["verified"])


class AppVersionColumnTests(unittest.TestCase):
    def test_migrates_a_database_predating_the_app_version_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "db.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE test_runs (id TEXT PRIMARY KEY, device_serial TEXT NOT NULL, status TEXT NOT NULL,"
                " started_at TEXT, finished_at TEXT, checkpoint TEXT, error TEXT)"
            )
            conn.commit()
            conn.close()

            storage = Storage(db_path)
            storage.initialize()
            storage.create_run("run1", "device")
            storage.set_run_app_version(
                "run1", {"package": "com.example", "version_name": "1.2.3", "version_code": 45})
            run = storage.get_run("run1")
            self.assertEqual(run["app_package"], "com.example")
            self.assertEqual(run["app_version_name"], "1.2.3")
            self.assertEqual(run["app_version_code"], 45)

    def test_setting_no_version_leaves_the_columns_null(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            storage.set_run_app_version("run1", None)
            self.assertIsNone(storage.get_run("run1")["app_version_name"])


class CampaignStorageTests(unittest.TestCase):
    def test_migrates_database_predating_campaign_id_column(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "db.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE test_runs (id TEXT PRIMARY KEY, device_serial TEXT NOT NULL, status TEXT NOT NULL,"
                " started_at TEXT, finished_at TEXT, checkpoint TEXT, error TEXT)"
            )
            conn.execute("INSERT INTO test_runs VALUES ('legacy','devX','completed',null,null,null,null)")
            conn.commit()
            conn.close()

            storage = Storage(db_path)
            storage.initialize()
            storage.initialize()  # the added index must not break a second pass
            self.assertIsNotNone(storage.get_run("legacy"))
            self.assertIsNone(storage.get_run("legacy")["campaign_id"])

    def test_lists_campaigns_with_child_run_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_campaign("c1", "repeat", "devA", 30.0, tier="smoke", iterations=2)
            for index, status in enumerate([RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PENDING]):
                storage.create_run(f"r{index}", "devA", "cold_start", campaign_id="c1")
                storage.update_run(f"r{index}", status)

            campaign = storage.list_campaigns()[0]
            self.assertEqual(campaign["run_count"], 3)
            self.assertEqual(campaign["completed_count"], 1)
            self.assertEqual(campaign["failed_count"], 1)
            self.assertEqual([r["id"] for r in storage.list_campaign_runs("c1")], ["r0", "r1", "r2"])

    def test_cancel_flags_only_unfinished_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_campaign("c1", "repeat", "devA", 30.0, scenario="cold_start", iterations=3)
            storage.create_run("done", "devA", campaign_id="c1")
            storage.update_run("done", RunStatus.COMPLETED)
            storage.create_run("queued", "devA", campaign_id="c1")

            self.assertEqual(storage.cancel_campaign_runs("c1"), 1)
            self.assertEqual(storage.get_run("queued")["cancel_requested"], 1)
            self.assertEqual(storage.get_run("done")["cancel_requested"], 0)

    def test_delete_campaign_removes_its_runs_and_returns_their_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_campaign("c1", "soak", "devA", 3600.0, scenario="play_golden")
            storage.create_run("child", "devA", campaign_id="c1")
            storage.create_run("unrelated", "devA")

            self.assertEqual(storage.delete_campaign("c1"), ["child"])
            self.assertIsNone(storage.get_campaign("c1"))
            self.assertIsNone(storage.get_run("child"))
            self.assertIsNotNone(storage.get_run("unrelated"))


if __name__ == "__main__":
    unittest.main()


class RunOriginTests(unittest.TestCase):
    """Who asked for a run -- a question the table could not answer at all.

    A run started with `autoperf run` and one queued from the dashboard wrote
    byte-identical rows, so "show me only what a person triggered by hand" had
    no answer, and a pass rate mixed attended and unattended evidence with no
    way to notice.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = Storage(Path(self.directory.name) / "t.db")
        self.storage.initialize()

    def test_origin_round_trips(self):
        self.storage.create_run("r1", "S1", origin=RunOrigin.DASHBOARD)
        self.assertEqual(self.storage.get_run("r1")["origin"], "dashboard")

    def test_an_unstated_origin_is_recorded_as_unknown_not_guessed(self):
        # Rows written before the column existed have no recoverable origin,
        # so the default has to be a value that says so rather than one that
        # invents provenance next to measured numbers.
        self.storage.create_run("r1", "S1")
        self.assertEqual(self.storage.get_run("r1")["origin"], "unknown")

    def test_runs_can_be_listed_by_origin(self):
        self.storage.create_run("r1", "S1", origin=RunOrigin.MANUAL)
        self.storage.create_run("r2", "S1", origin=RunOrigin.CAMPAIGN)
        self.storage.create_run("r3", "S1", origin=RunOrigin.MANUAL)
        listed = {row["id"] for row in self.storage.list_runs(origin=RunOrigin.MANUAL)}
        self.assertEqual(listed, {"r1", "r3"})

    def test_origin_counts_are_a_facet_over_every_run(self):
        self.storage.create_run("r1", "S1", origin=RunOrigin.MANUAL)
        self.storage.create_run("r2", "S1", origin=RunOrigin.CAMPAIGN)
        self.storage.create_run("r3", "S2", origin=RunOrigin.MANUAL)
        self.assertEqual(self.storage.run_origin_counts(), {"manual": 2, "campaign": 1})
        self.assertEqual(self.storage.run_origin_counts("S2"), {"manual": 1})

    def test_the_column_is_added_to_a_database_that_predates_it(self):
        """The migration has to work on a file that already holds runs."""
        path = Path(self.directory.name) / "old.db"
        with closing(sqlite3.connect(path)) as conn:
            with conn:
                conn.execute("CREATE TABLE test_runs (id TEXT PRIMARY KEY, "
                             "device_serial TEXT NOT NULL, status TEXT NOT NULL)")
                conn.execute("INSERT INTO test_runs VALUES ('old', 'S1', 'completed')")
        storage = Storage(path)
        storage.initialize()
        self.assertEqual(storage.get_run("old")["origin"], "unknown")
