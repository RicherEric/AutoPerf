import tempfile
import threading
import time
import unittest
from pathlib import Path

from autoperf.adapters import HOME, Adapter, AndroidAdapter, ScenarioStep
from autoperf.collectors import Collector, CpuCollector
from autoperf.runner import DeviceBusyError, TestRunner
from autoperf.storage import Storage
from tests.support import DeviceAdb


def FakeAdb():
    return DeviceAdb(metrics=True)


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


class CountingStorage(Storage):
    """Storage that records how often a run's checkpoint is written."""

    def __init__(self, path):
        super().__init__(path)
        self.running_updates = 0

    def update_run(self, run_id, status, *, checkpoint=None, error=None):
        if status == "running":
            self.running_updates += 1
        return super().update_run(run_id, status, checkpoint=checkpoint, error=error)


class HeartbeatThrottleTests(unittest.TestCase):
    def test_checkpoint_writes_are_throttled_not_once_per_control_tick(self):
        """Checkpoint cost must not scale with run length.

        The control loop ticks every ~10ms so collector timeouts stay
        responsive. Writing the checkpoint on every tick meant ~100 SQLite
        write transactions per second, each opening its own connection --
        survivable for a 60-second run, fatal for a multi-hour soak, where it
        starves BatchWriter of the single WAL write lock until its bounded
        queue overflows. With a 0.25s heartbeat a ~1s run must produce only a
        handful of checkpoint writes, not ~100.
        """
        with tempfile.TemporaryDirectory() as directory:
            storage = CountingStorage(Path(directory) / "db.sqlite")
            storage.initialize()
            runner = TestRunner(
                storage, FakeAdb(), [CpuCollector(interval=0.05)], heartbeat_interval=0.25
            )
            run_id = runner.run("serial", 1.0)

            self.assertLessEqual(storage.running_updates, 10)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")

    def test_final_checkpoint_records_elapsed_time_even_below_one_heartbeat(self):
        # Throttling must not cost a run its checkpoint: a run shorter than a
        # single heartbeat interval still has to record where it got to, or
        # `--resume` has nothing to resume from.
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            runner = TestRunner(
                storage, FakeAdb(), [CpuCollector(interval=0.05)], heartbeat_interval=60.0
            )
            run_id = runner.run("serial", 0.2)

            checkpoint = storage.get_run(run_id)["checkpoint"]
            self.assertIsNotNone(checkpoint)
            self.assertGreater(float(checkpoint), 0.0)


class VerificationRecordingTests(unittest.TestCase):
    """The point of the whole verification layer, end to end.

    Before it, a scenario step that achieved nothing was recorded as
    "adapter_action completed" and the run finished green -- indistinguishable
    from a run that worked. These assert that the three outcomes are now
    distinguishable in the stored record.
    """

    def _events(self, storage, run_id):
        import sqlite3
        from contextlib import closing

        # `with sqlite3.connect(...)` opens a *transaction*, not a closing
        # scope -- the connection would stay open and Windows would refuse to
        # delete the temp directory afterwards.
        with closing(sqlite3.connect(str(storage.path))) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT kind, message, details FROM test_events WHERE run_id=?", (run_id,))]

    def _run_with_step(self, directory, adapter, step):
        storage = Storage(Path(directory) / "db.sqlite")
        storage.initialize()
        runner = TestRunner(
            storage, FakeAdb(), [CpuCollector(interval=0.05)],
            adapter=adapter, scenario=[step], heartbeat_interval=0.25,
        )
        return storage, runner.run("serial", 0.6)

    def test_a_verification_failure_is_recorded_and_marks_the_run_unverified(self):
        from autoperf.adapters import VerificationError

        class FailingAdapter(AndroidAdapter):
            def verify_foreground(self, adb, serial, package):
                raise VerificationError(f"expected {package} in foreground, found com.android.launcher")

        with tempfile.TemporaryDirectory() as directory:
            storage, run_id = self._run_with_step(
                directory, FailingAdapter(),
                ScenarioStep(0.0, "verify_foreground", {"package": "com.example.app"}),
            )
            kinds = [e["kind"] for e in self._events(storage, run_id)]
            self.assertIn("verification_failed", kinds)
            # Crucially it is NOT recorded as a completed action...
            self.assertNotIn("adapter_action", kinds)
            quality = storage.run_quality(run_id)
            self.assertFalse(quality["verified"])
            self.assertEqual(quality["verification_failures"], 1)

    def test_a_coordinate_fallback_is_recorded_without_failing_the_run(self):
        class FallbackAdapter(AndroidAdapter):
            def tap_element(self, adb, serial, target, screen=None):
                return {"target": "search_icon", "strategy": "coordinates", "x": 1, "y": 2}

        with tempfile.TemporaryDirectory() as directory:
            storage, run_id = self._run_with_step(
                directory, FallbackAdapter(),
                ScenarioStep(0.0, "tap_element", {"target": None}),
            )
            kinds = [e["kind"] for e in self._events(storage, run_id)]
            # A fallback still worked, so the action completed -- but the
            # decay is on the record either way.
            self.assertIn("selector_fallback", kinds)
            self.assertIn("adapter_action", kinds)
            quality = storage.run_quality(run_id)
            self.assertTrue(quality["verified"])
            self.assertEqual(quality["selector_fallbacks"], 1)

    def test_a_selector_hit_records_neither_a_failure_nor_a_fallback(self):
        class GoodAdapter(AndroidAdapter):
            def tap_element(self, adb, serial, target, screen=None):
                return {"target": "search_icon", "strategy": "content_desc", "x": 1, "y": 2}

        with tempfile.TemporaryDirectory() as directory:
            storage, run_id = self._run_with_step(
                directory, GoodAdapter(),
                ScenarioStep(0.0, "tap_element", {"target": None}),
            )
            quality = storage.run_quality(run_id)
            self.assertTrue(quality["verified"])
            self.assertEqual(quality["selector_fallbacks"], 0)

    def test_non_serialisable_step_kwargs_do_not_break_the_writer(self):
        # Step kwargs now carry Target objects, which json.dumps cannot
        # handle; the batch writer would die mid-run if they reached it raw.
        from autoperf.uiauto import Selector, Target

        target = Target((Selector(content_desc="Search"),), (0.5, 0.5), "search_icon")

        class GoodAdapter(AndroidAdapter):
            def tap_element(self, adb, serial, target, screen=None):
                return {"target": target.name, "strategy": "content_desc", "x": 1, "y": 2}

        with tempfile.TemporaryDirectory() as directory:
            storage, run_id = self._run_with_step(
                directory, GoodAdapter(), ScenarioStep(0.0, "tap_element", {"target": target}),
            )
            self.assertEqual(storage.get_run(run_id)["status"], "completed")
            details = [e["details"] for e in self._events(storage, run_id) if e["kind"] == "adapter_action"]
            self.assertTrue(any("search_icon" in d for d in details))


class CancelStopsTheDeviceFirstTests(unittest.TestCase):
    """Cancelling must quiet the device before waiting on in-flight work.

    `cancel_futures` only drops futures that have not started; a running one
    cannot be interrupted, and an element-locating step can spend seconds
    dumping the UI and retrying. Measured on a Galaxy A55 before the fix: a
    cancel landing mid-`tap_element` left the video playing for 10.2 seconds
    while shutdown() waited. Afterwards the device went quiet in 1.0s.
    """

    class SlowAdapter(AndroidAdapter):
        def __init__(self, hold: float):
            super().__init__()
            self.hold = hold
            self.events = []

        def tap_element(self, adb, serial, target, screen=None):
            self.events.append("tap_element:start")
            time.sleep(self.hold)
            self.events.append("tap_element:end")
            return {"target": "t", "strategy": "content_desc", "x": 1, "y": 2}

        def stop_app(self, adb, serial, package):
            self.events.append("stop_app")

        def key_event(self, adb, serial, keycode):
            self.events.append(f"key_event:{keycode}")

    def _cancel_during_step(self, hold):
        storage = Storage(Path(self._dir) / "db.sqlite")
        storage.initialize()
        storage.create_run("r1", "serial")
        adapter = self.SlowAdapter(hold)
        scenario = [
            ScenarioStep(0.0, "launch_app", {"package": "com.example.app"}),
            ScenarioStep(0.2, "tap_element", {"target": None}),
        ]
        runner = TestRunner(storage, FakeAdb(), [CpuCollector(interval=0.05)],
                            adapter=adapter, scenario=scenario, heartbeat_interval=0.25)

        def cancel_soon():
            time.sleep(0.5)
            storage.request_cancel("r1")

        threading.Thread(target=cancel_soon, daemon=True).start()
        runner.run("serial", 30.0, "r1")
        return storage, adapter

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_the_app_is_stopped_before_the_slow_step_finishes(self):
        storage, adapter = self._cancel_during_step(hold=3.0)

        self.assertEqual(storage.get_run("r1")["status"], "interrupted")
        stop = adapter.events.index("stop_app")
        end = adapter.events.index("tap_element:end")
        # The whole point: quieting the device does not queue behind the
        # action that is still running.
        self.assertLess(stop, end, f"stop_app came after the in-flight step: {adapter.events}")

    def test_the_device_is_left_on_the_home_screen(self):
        # A tap already in flight when the app was stopped lands afterwards,
        # on whatever is now on screen -- observed leaving the Galaxy Store
        # open on a real phone.
        _, adapter = self._cancel_during_step(hold=1.0)
        self.assertEqual(adapter.events[-1], f"key_event:{HOME}")

    def test_a_normal_finish_still_stops_the_app_exactly_once(self):
        storage = Storage(Path(self._dir) / "db.sqlite")
        storage.initialize()
        adapter = self.SlowAdapter(0.0)
        runner = TestRunner(
            storage, FakeAdb(), [CpuCollector(interval=0.05)], adapter=adapter,
            scenario=[ScenarioStep(0.0, "launch_app", {"package": "com.example.app"})],
            heartbeat_interval=0.25,
        )
        run_id = runner.run("serial", 0.4)

        self.assertEqual(storage.get_run(run_id)["status"], "completed")
        self.assertEqual(adapter.events.count("stop_app"), 1)
        self.assertNotIn(f"key_event:{HOME}", adapter.events)


class IntrospectionRecordingTests(unittest.TestCase):
    """The tool's own cost has to end up in the run's record.

    Locating elements by identity is right, and it is not free: the dumps land
    on the same CPU the collectors are sampling. A run with many lookups and a
    run with none are not comparable, and without this event nothing in the
    data says which is which.
    """

    def test_a_lookup_records_what_it_cost(self):
        from autoperf.scenarios import selectors

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            scenario = [
                ScenarioStep(0.0, "launch_app", {"package": "com.example.app"}),
                ScenarioStep(0.0, "tap_element", {"target": selectors.SEARCH_ICON}),
            ]
            run_id = TestRunner(
                storage, DeviceAdb(metrics=True), [CpuCollector(interval=0.05)],
                adapter=AndroidAdapter(), scenario=scenario, heartbeat_interval=0.25,
            ).run("serial", 0.4)

            quality = storage.run_quality(run_id)
            self.assertEqual(quality["ui_introspections"], 1)
            # Not a failure and not a warning: it is the cost of locating by
            # identity, reported so two runs can be told apart.
            self.assertTrue(quality["verified"])

    def test_a_run_with_no_lookups_records_none(self):
        # A deep-link scenario touches nothing, which is exactly why those are
        # the right presets for baseline comparison.
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            run_id = TestRunner(
                storage, DeviceAdb(metrics=True), [CpuCollector(interval=0.05)],
                adapter=AndroidAdapter(), heartbeat_interval=0.25,
                scenario=[ScenarioStep(0.0, "launch_app", {"package": "com.example.app"})],
            ).run("serial", 0.4)
            self.assertEqual(storage.run_quality(run_id)["ui_introspections"], 0)


class AppVersionRecordingTests(unittest.TestCase):
    def test_records_the_launched_package_version(self):
        # A device that can report its build, which is one more reply rather
        # than one more class.
        version_adb = DeviceAdb(metrics=True, replies={
            "dumpsys package com.example.app": "  versionCode=1543012928\n  versionName=19.09.37\n",
        })
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            TestRunner(
                storage, version_adb, [CpuCollector(interval=0.05)],
                adapter=AndroidAdapter(),
                scenario=[ScenarioStep(0.0, "launch_app", {"package": "com.example.app"})],
                heartbeat_interval=0.25,
            ).run("serial", 0.4)

            run = storage.list_runs()[0]
            self.assertEqual(run["app_package"], "com.example.app")
            self.assertEqual(run["app_version_name"], "19.09.37")
            self.assertEqual(run["app_version_code"], 1543012928)

    def test_an_unreadable_version_does_not_fail_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite")
            storage.initialize()
            run_id = TestRunner(
                storage, FakeAdb(), [CpuCollector(interval=0.05)],
                adapter=AndroidAdapter(),
                scenario=[ScenarioStep(0.0, "launch_app", {"package": "com.example.app"})],
                heartbeat_interval=0.25,
            ).run("serial", 0.4)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")
            self.assertIsNone(storage.get_run(run_id)["app_version_name"])


if __name__ == "__main__":
    unittest.main()
