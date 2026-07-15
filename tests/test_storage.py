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


if __name__ == "__main__":
    unittest.main()
