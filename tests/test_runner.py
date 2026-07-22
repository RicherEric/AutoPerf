import tempfile
import threading
import time
import unittest
from pathlib import Path

from autoperf.adapters import HOME, Adapter, AndroidAdapter, ScenarioStep
from autoperf.collectors import Collector, CpuCollector
from autoperf.runner import DeviceBusyError, TestRunner
from autoperf.storage import Storage


class FakeAdb:
    def shell(self, serial, command, timeout=10):
        return {
            "dumpsys cpuinfo": "1.0% TOTAL: 1.0% user + 0.0% kernel",
            "monkey -p com.example.app -c android.intent.category.LAUNCHER 1": "",
        }[command]


class BoomCollector(Collector):
    def __init__(self):
        super().__init__(0.01, "boom")

    def collect(self, adb, serial, run_id):
        raise RuntimeError("boom!")


class BoomAdapter(Adapter):
    def __init__(self):
        super().__init__("boom")

    def launch_app(self, adb, serial, package, activity=None):
        raise RuntimeError("boom!")

    def stop_app(self, adb, serial, package):
        raise NotImplementedError

    def tap(self, adb, serial, x, y):
        raise NotImplementedError

    def swipe(self, adb, serial, x1, y1, x2, y2, duration_ms=300):
        raise NotImplementedError

    def key_event(self, adb, serial, keycode):
        raise NotImplementedError

    def screen_size(self, adb, serial):
        raise NotImplementedError


class RecordingAdapter(AndroidAdapter):
    def __init__(self):
        super().__init__()
        self.key_events = []
        self.stopped_apps = []

    def key_event(self, adb, serial, keycode):
        self.key_events.append(keycode)

    def stop_app(self, adb, serial, package):
        self.stopped_apps.append(package)


class RunnerTests(unittest.TestCase):
    def test_scenario_without_adapter_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            scenario = [ScenarioStep(0.0, "launch_app", {"package": "com.example.app"})]
            runner = TestRunner(storage, FakeAdb(), [CpuCollector(0.01)], scenario=scenario)
            with self.assertRaises(ValueError):
                runner.run("device", 0.02)

    def test_scenario_with_unknown_action_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            scenario = [ScenarioStep(0.0, "not_a_real_action", {})]
            runner = TestRunner(storage, FakeAdb(), [CpuCollector(0.01)], adapter=AndroidAdapter(), scenario=scenario)
            with self.assertRaises(ValueError):
                runner.run("device", 0.02)

    def test_run_with_existing_run_id_for_different_device_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "deviceA")
            runner = TestRunner(storage, FakeAdb(), [CpuCollector(0.01)])
            with self.assertRaises(ValueError):
                runner.run("deviceB", 0.02, run_id="run1")

    def test_run_raises_device_busy_when_another_run_on_same_device_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            storage.create_run("run1", "device")
            storage.try_start_run("run1")
            storage.create_run("run2", "device")
            runner = TestRunner(storage, FakeAdb(), [CpuCollector(0.01)])
            with self.assertRaises(DeviceBusyError):
                runner.run("device", 0.02, run_id="run2")

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

    def test_adapter_exception_is_recorded_as_event_and_run_still_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            scenario = [ScenarioStep(0.0, "launch_app", {"package": "com.example.app"})]
            run_id = TestRunner(
                storage, FakeAdb(), [CpuCollector(0.01)], adapter=BoomAdapter(), scenario=scenario
            ).run("device", 0.05)
            conn = storage.connect()
            try:
                kinds = {row[0] for row in conn.execute(
                    "SELECT kind FROM test_events WHERE run_id=?", (run_id,)
                )}
                names = {row[0] for row in conn.execute(
                    "SELECT name FROM metric_samples WHERE run_id=?", (run_id,)
                )}
            finally:
                conn.close()
            self.assertIn("adapter_error", kinds)
            self.assertIn("cpu.total", names)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")

    def test_successful_adapter_action_is_recorded_as_event(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            scenario = [ScenarioStep(0.0, "launch_app", {"package": "com.example.app"})]
            run_id = TestRunner(
                storage, FakeAdb(), [CpuCollector(0.01)], adapter=AndroidAdapter(), scenario=scenario
            ).run("device", 0.05)
            conn = storage.connect()
            try:
                row = conn.execute(
                    "SELECT kind, details FROM test_events WHERE run_id=? AND kind='adapter_action'", (run_id,)
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "adapter_action")
            self.assertIn('"action": "launch_app"', row[1])
            self.assertIn('"package": "com.example.app"', row[1])

    def test_cancelled_mid_run_presses_home_before_marking_interrupted(self):
        # No scenario is set here, so there's no package to force-stop --
        # cleanup falls back to pressing Home (see
        # test_completed_run_force_stops_the_scenarios_app for the more
        # common case where a scenario's app gets force-stopped instead).
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            run_id = "cancel-me"
            adapter = RecordingAdapter()

            def cancel_soon():
                time.sleep(0.05)
                storage.request_cancel(run_id)

            threading.Thread(target=cancel_soon, daemon=True).start()
            TestRunner(
                storage, FakeAdb(), [CpuCollector(0.01)], adapter=adapter, cancel_check_interval=0.02,
            ).run("device", 5.0, run_id)

            self.assertIn(HOME, adapter.key_events)
            self.assertEqual(storage.get_run(run_id)["status"], "interrupted")

    def test_completed_run_force_stops_the_scenarios_app(self):
        # A normally-completed scenario run still leaves whatever app was
        # driven (e.g. a video mid-playback) on screen -- every run, not
        # just a cancelled one, should end with it cleaned up. Force-stopping
        # (not just pressing Home) is what's needed here: many apps, YouTube
        # included, keep playing in the background once merely backgrounded.
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            adapter = RecordingAdapter()
            scenario = [ScenarioStep(0.0, "launch_app", {"package": "com.example.app"})]
            run_id = TestRunner(
                storage, FakeAdb(), [CpuCollector(0.01)], adapter=adapter, scenario=scenario
            ).run("device", 0.05)

            self.assertIn("com.example.app", adapter.stopped_apps)
            self.assertNotIn(HOME, adapter.key_events)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")

    def test_run_without_adapter_never_presses_home(self):
        # A plain (non-scenario) run has no adapter at all -- key_event must
        # never be reached for it.
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            run_id = TestRunner(storage, FakeAdb(), [CpuCollector(0.01)]).run("device", 0.05)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
