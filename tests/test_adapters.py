import unittest

from autoperf.adapters import HOME, AndroidAdapter, AndroidTvAdapter


class RecordingAdb:
    def __init__(self, response=""):
        self.response = response
        self.calls = []

    def shell(self, serial, command, timeout=10):
        self.calls.append((serial, command, timeout))
        return self.response


class AndroidAdapterTests(unittest.TestCase):
    def test_launch_app_without_activity_uses_monkey(self):
        adb = RecordingAdb()
        AndroidAdapter().launch_app(adb, "device", "com.example.app")
        self.assertEqual(adb.calls, [("device", "monkey -p com.example.app -c android.intent.category.LAUNCHER 1", 10)])

    def test_launch_app_with_activity_uses_am_start(self):
        adb = RecordingAdb()
        AndroidAdapter().launch_app(adb, "device", "com.example.app", "com.example.app.MainActivity")
        self.assertEqual(adb.calls, [("device", "am start -n com.example.app/com.example.app.MainActivity", 10)])

    def test_stop_app(self):
        adb = RecordingAdb()
        AndroidAdapter().stop_app(adb, "device", "com.example.app")
        self.assertEqual(adb.calls, [("device", "am force-stop com.example.app", 10)])

    def test_tap(self):
        adb = RecordingAdb()
        AndroidAdapter().tap(adb, "device", 100, 200)
        self.assertEqual(adb.calls, [("device", "input tap 100 200", 10)])

    def test_swipe_uses_default_duration(self):
        adb = RecordingAdb()
        AndroidAdapter().swipe(adb, "device", 100, 200, 300, 400)
        self.assertEqual(adb.calls, [("device", "input swipe 100 200 300 400 300", 10)])

    def test_swipe_uses_custom_duration(self):
        adb = RecordingAdb()
        AndroidAdapter().swipe(adb, "device", 100, 200, 300, 400, duration_ms=500)
        self.assertEqual(adb.calls, [("device", "input swipe 100 200 300 400 500", 10)])

    def test_key_event(self):
        adb = RecordingAdb()
        AndroidAdapter().key_event(adb, "device", HOME)
        self.assertEqual(adb.calls, [("device", "input keyevent KEYCODE_HOME", 10)])

    def test_launch_app_rejects_invalid_package_without_calling_shell(self):
        adb = RecordingAdb()
        with self.assertRaises(ValueError):
            AndroidAdapter().launch_app(adb, "device", "com.example; reboot")
        self.assertEqual(adb.calls, [])

    def test_launch_app_with_data_uses_view_intent(self):
        adb = RecordingAdb()
        AndroidAdapter().launch_app(adb, "device", "com.example.app", data="https://www.youtube.com/watch?v=abc123")
        self.assertEqual(adb.calls, [(
            "device",
            'am start -a android.intent.action.VIEW -d "https://www.youtube.com/watch?v=abc123" com.example.app',
            10,
        )])

    def test_launch_app_data_takes_precedence_over_activity(self):
        adb = RecordingAdb()
        AndroidAdapter().launch_app(adb, "device", "com.example.app", activity="com.example.app.MainActivity",
                                     data="https://example.com/x")
        self.assertEqual(adb.calls, [(
            "device", 'am start -a android.intent.action.VIEW -d "https://example.com/x" com.example.app', 10,
        )])

    def test_launch_app_rejects_invalid_data_without_calling_shell(self):
        adb = RecordingAdb()
        with self.assertRaises(ValueError):
            AndroidAdapter().launch_app(adb, "device", "com.example.app", data='https://x"; reboot; echo "')
        self.assertEqual(adb.calls, [])

    def test_launch_app_rejects_invalid_activity_without_calling_shell(self):
        adb = RecordingAdb()
        with self.assertRaises(ValueError):
            AndroidAdapter().launch_app(adb, "device", "com.example.app", "; reboot")
        self.assertEqual(adb.calls, [])

    def test_key_event_rejects_invalid_keycode_without_calling_shell(self):
        adb = RecordingAdb()
        with self.assertRaises(ValueError):
            AndroidAdapter().key_event(adb, "device", "; reboot")
        self.assertEqual(adb.calls, [])

    def test_tap_rejects_non_numeric_coordinates_without_calling_shell(self):
        adb = RecordingAdb()
        with self.assertRaises(ValueError):
            AndroidAdapter().tap(adb, "device", "100; reboot", 200)
        self.assertEqual(adb.calls, [])

    def test_screen_size_parses_physical_size(self):
        adb = RecordingAdb(response="Physical size: 1080x2340\nOverride size: 1080x2340\n")
        self.assertEqual(AndroidAdapter().screen_size(adb, "device"), (1080, 2340))
        self.assertEqual(adb.calls, [("device", "wm size", 10)])

    def test_screen_size_raises_on_unparsable_output(self):
        adb = RecordingAdb(response="no size info here")
        with self.assertRaises(ValueError):
            AndroidAdapter().screen_size(adb, "device")

    def test_tv_adapter_maps_settings_to_tv_activity(self):
        adb = RecordingAdb()
        AndroidTvAdapter().launch_app(adb, "tv", "com.android.settings")
        self.assertEqual(
            adb.calls,
            [("tv", "am start -n com.android.tv.settings/.MainSettings", 10)],
        )

    def test_tv_adapter_maps_youtube_package(self):
        adb = RecordingAdb()
        AndroidTvAdapter().launch_app(adb, "tv", "com.google.android.youtube")
        self.assertEqual(
            adb.calls,
            [("tv", "monkey -p com.google.android.youtube.tv -c android.intent.category.LAUNCHER 1", 10)],
        )

    def test_tv_adapter_maps_touch_gestures_to_dpad(self):
        adb = RecordingAdb()
        adapter = AndroidTvAdapter()
        adapter.tap(adb, "tv", 100, 200)
        adapter.swipe(adb, "tv", 500, 800, 500, 300)
        self.assertEqual(adb.calls, [
            ("tv", "input keyevent KEYCODE_DPAD_CENTER", 10),
            ("tv", "input keyevent KEYCODE_DPAD_DOWN", 10),
        ])


class SelectAdapterTests(unittest.TestCase):
    class _Props:
        def __init__(self, value, fail=False):
            self.value, self.fail = value, fail

        def shell(self, serial, command, timeout=10):
            if self.fail:
                raise RuntimeError("device offline")
            return self.value

    def _select(self, value):
        from autoperf.adapters import select_adapter

        return select_adapter(self._Props(value), "SERIAL1")

    def test_detects_tv_despite_the_trailing_newline_adb_returns(self):
        """AdbClient._run returns adb's stdout verbatim, newline included.

        `ro.build.characteristics` is very often exactly "tv", so the last
        (here only) comma-separated entry arrives as "tv\\n". Comparing
        without stripping missed it and silently handed the device the phone
        adapter, which sends coordinate taps to hardware that only answers to
        DPAD keys -- the scenario would appear to run and do nothing.
        """
        from autoperf.adapters import AndroidTvAdapter

        for value in ("tv\n", "tv", "tv,nosdcard\n", "nosdcard,tv\n", "TV\n"):
            with self.subTest(value=value):
                self.assertIsInstance(self._select(value), AndroidTvAdapter)

    def test_non_tv_devices_get_the_generic_adapter(self):
        from autoperf.adapters import AndroidAdapter, AndroidTvAdapter

        for value in ("phone\n", "default\n", "nosdcard\n", "\n", ""):
            with self.subTest(value=value):
                adapter = self._select(value)
                self.assertIsInstance(adapter, AndroidAdapter)
                self.assertNotIsInstance(adapter, AndroidTvAdapter)

    def test_unreadable_property_falls_back_to_the_generic_adapter(self):
        from autoperf.adapters import AndroidAdapter, select_adapter

        adapter = select_adapter(self._Props("", fail=True), "SERIAL1")
        self.assertIsInstance(adapter, AndroidAdapter)


if __name__ == "__main__":
    unittest.main()
