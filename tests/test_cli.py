import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoperf import cli
from autoperf.models import Device, MetricSample
from autoperf.storage import BatchWriter, Storage


class FakeAdb:
    def devices(self):
        return [Device("SERIAL1", "device", "Pixel", "pixel")]

    def shell(self, serial, command, timeout=10):
        return {
            "dumpsys cpuinfo": "1.0% TOTAL: 1.0% user + 0.0% kernel",
            "cat /proc/meminfo": "MemTotal: 100 kB\nMemAvailable: 50 kB\n",
            "dumpsys battery": " level: 50\n temperature: 300\n",
            "monkey -p com.example.app -c android.intent.category.LAUNCHER 1": "",
        }[command]


class CliTests(unittest.TestCase):
    def test_status_reports_missing_run_and_returns_error_code(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "status", "missing-run"])
            self.assertEqual(code, 1)
            self.assertIn("Run not found", out.getvalue())

    def test_devices_command_registers_and_prints_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with patch("autoperf.cli.AdbClient", return_value=FakeAdb()):
                with contextlib.redirect_stdout(out):
                    code = cli.main(["--db", str(db), "devices"])
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(out.getvalue()),
                [{"serial": "SERIAL1", "state": "device", "model": "Pixel"}],
            )
            storage = Storage(db)
            conn = storage.connect()
            try:
                row = conn.execute("SELECT model FROM devices WHERE serial=?", ("SERIAL1",)).fetchone()
            finally:
                conn.close()
            self.assertEqual(row, ("Pixel",))

    def test_run_command_executes_and_prints_completed_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with patch("autoperf.cli.AdbClient", return_value=FakeAdb()):
                with contextlib.redirect_stdout(out):
                    code = cli.main(["--db", str(db), "run", "--serial", "SERIAL1", "--duration", "0.05"])
            self.assertEqual(code, 0)
            run_id = out.getvalue().strip()
            storage = Storage(db)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")

    def test_run_command_with_app_flag_drives_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with patch("autoperf.cli.AdbClient", return_value=FakeAdb()):
                with contextlib.redirect_stdout(out):
                    code = cli.main([
                        "--db", str(db), "run", "--serial", "SERIAL1", "--duration", "0.05",
                        "--app", "com.example.app",
                    ])
            self.assertEqual(code, 0)
            run_id = out.getvalue().strip()
            storage = Storage(db)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")
            conn = storage.connect()
            try:
                kinds = {row[0] for row in conn.execute(
                    "SELECT kind FROM test_events WHERE run_id=?", (run_id,)
                )}
            finally:
                conn.close()
            self.assertIn("adapter_action", kinds)

    def _seed_run_with_samples(self, storage, run_id, serial, values):
        storage.create_run(run_id, serial)
        writer = BatchWriter(storage)
        writer.start()
        for value in values:
            writer.put(MetricSample(run_id, "cpu", "cpu.total", value, "%"))
        writer.close()

    def test_baseline_set_rejects_run_from_a_different_device(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            storage = Storage(db)
            storage.initialize()
            storage.create_run("run1", "OTHER_SERIAL")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "baseline", "set", "--serial", "SERIAL1", "--run", "run1"])
            self.assertEqual(code, 1)

    def test_baseline_set_and_show_returns_computed_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            storage = Storage(db)
            storage.initialize()
            self._seed_run_with_samples(storage, "run1", "SERIAL1", [10.0, 20.0])

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "baseline", "set", "--serial", "SERIAL1", "--run", "run1"])
            self.assertEqual(code, 0)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "baseline", "show", "--serial", "SERIAL1"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["run_id"], "run1")
            self.assertEqual(payload["stats"]["cpu.total"]["mean"], 15.0)

    def test_compare_without_baseline_returns_error(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            storage = Storage(db)
            storage.initialize()
            self._seed_run_with_samples(storage, "run1", "SERIAL1", [10.0])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "compare", "--run", "run1"])
            self.assertEqual(code, 1)
            self.assertIn("No baseline set", out.getvalue())

    def test_compare_flags_regression_against_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            storage = Storage(db)
            storage.initialize()
            self._seed_run_with_samples(storage, "baseline_run", "SERIAL1", [10.0])
            self._seed_run_with_samples(storage, "candidate_run", "SERIAL1", [50.0])
            storage.set_baseline("SERIAL1", "baseline_run")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "compare", "--run", "candidate_run"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["regressed"])
            self.assertEqual(payload["metrics"][0]["name"], "cpu.total")


if __name__ == "__main__":
    unittest.main()
