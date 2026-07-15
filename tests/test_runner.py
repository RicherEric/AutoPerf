import tempfile
import unittest
from pathlib import Path

from autoperf.collectors import Collector, CpuCollector
from autoperf.runner import TestRunner
from autoperf.storage import Storage


class FakeAdb:
    def shell(self, serial, command, timeout=10):
        return {
            "dumpsys cpuinfo": "1.0% TOTAL: 1.0% user + 0.0% kernel",
        }[command]


class BoomCollector(Collector):
    def __init__(self):
        super().__init__(0.01, "boom")

    def collect(self, adb, serial, run_id):
        raise RuntimeError("boom!")


class RunnerTests(unittest.TestCase):
    def test_run_with_existing_run_id_for_different_device_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "deviceA")
            runner = TestRunner(storage, FakeAdb(), [CpuCollector(0.01)])
            with self.assertRaises(ValueError):
                runner.run("deviceB", 0.02, run_id="run1")

    def test_resume_with_same_device_reuses_run(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            runner = TestRunner(storage, FakeAdb(), [CpuCollector(0.01)])
            first = runner.run("device", 0.02, run_id="run1")
            second = runner.run("device", 0.02, run_id="run1")
            self.assertEqual(first, second)
            self.assertEqual(storage.get_run("run1")["status"], "completed")

    def test_collector_exception_is_recorded_as_event_and_run_still_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            run_id = TestRunner(storage, FakeAdb(), [BoomCollector()]).run("device", 0.05)
            conn = storage.connect()
            try:
                kinds = {row[0] for row in conn.execute(
                    "SELECT kind FROM test_events WHERE run_id=?", (run_id,)
                )}
            finally:
                conn.close()
            self.assertIn("collector_error", kinds)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
