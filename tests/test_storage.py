import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from autoperf.models import Device, MetricSample, RunStatus, TestEvent
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


if __name__ == "__main__":
    unittest.main()
