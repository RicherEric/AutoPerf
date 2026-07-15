import tempfile
import time
import unittest
from pathlib import Path

from autoperf.collectors import BatteryCollector, CpuCollector, MemoryCollector
from autoperf.runner import TestRunner as Runner
from autoperf.storage import Storage


class FakeAdb:
    def devices(self):
        return []

    def shell(self, serial, command, timeout=10):
        return {
            "dumpsys cpuinfo": "12.5% TOTAL: 8.0% user + 4.5% kernel",
            "cat /proc/meminfo": "MemTotal: 8000000 kB\nMemAvailable: 3000000 kB\n",
            "dumpsys battery": " level: 88\n temperature: 315\n",
        }[command]


class CoreTests(unittest.TestCase):
    def test_collectors_parse_android_output(self):
        adb = FakeAdb()
        samples = []
        for collector in (CpuCollector(), MemoryCollector(), BatteryCollector()):
            samples.extend(collector.collect(adb, "device", "run"))
        self.assertEqual({s.name: s.value for s in samples}, {
            "cpu.total": 12.5, "memory.used": 5000000.0,
            "battery.level": 88.0, "battery.temperature": 31.5,
        })

    def test_runner_persists_metrics_and_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            storage.initialize()
            collectors = [CpuCollector(0.01), MemoryCollector(0.01), BatteryCollector(0.01)]
            run_id = Runner(storage, FakeAdb(), collectors).run("device", 0.06)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")
            conn = storage.connect()
            try:
                count = conn.execute("SELECT count(*) FROM metric_samples WHERE run_id=?", (run_id,)).fetchone()[0]
                self.assertGreaterEqual(count, 4)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            finally:
                conn.close()

    def test_slow_collector_does_not_block_fast_collector(self):
        class SlowCollector(CpuCollector):
            def collect(self, adb, serial, run_id):
                time.sleep(0.08)
                return super().collect(adb, serial, run_id)

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "parallel.db")
            storage.initialize()
            run_id = Runner(
                storage, FakeAdb(), [SlowCollector(0.01), MemoryCollector(0.01)],
                collector_timeout=0.02,
            ).run("device", 0.05)
            conn = storage.connect()
            try:
                names = {row[0] for row in conn.execute(
                    "SELECT name FROM metric_samples WHERE run_id=?", (run_id,)
                )}
                events = {row[0] for row in conn.execute(
                    "SELECT kind FROM test_events WHERE run_id=?", (run_id,)
                )}
            finally:
                conn.close()
            self.assertIn("memory.used", names)
            self.assertIn("collector_timeout", events)


if __name__ == "__main__":
    unittest.main()
