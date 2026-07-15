import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoperf import cli
from autoperf.models import Device
from autoperf.storage import Storage


class FakeAdb:
    def devices(self):
        return [Device("SERIAL1", "device", "Pixel", "pixel")]

    def shell(self, serial, command, timeout=10):
        return {
            "dumpsys cpuinfo": "1.0% TOTAL: 1.0% user + 0.0% kernel",
            "cat /proc/meminfo": "MemTotal: 100 kB\nMemAvailable: 50 kB\n",
            "dumpsys battery": " level: 50\n temperature: 300\n",
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


if __name__ == "__main__":
    unittest.main()
