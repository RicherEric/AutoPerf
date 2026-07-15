import tempfile
import unittest
from pathlib import Path

from autoperf.storage import Storage
from autoperf.workers import DeviceSupervisor


class DeviceSupervisorValidationTests(unittest.TestCase):
    def test_run_many_requires_at_least_one_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            with self.assertRaises(ValueError):
                DeviceSupervisor(storage).run_many([], 1)

    def test_run_many_rejects_duplicate_serials(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            with self.assertRaises(ValueError):
                DeviceSupervisor(storage).run_many(["a", "a"], 1)


if __name__ == "__main__":
    unittest.main()
