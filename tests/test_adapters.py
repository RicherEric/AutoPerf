import unittest

from autoperf.adapters import HOME, AndroidAdapter


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


if __name__ == "__main__":
    unittest.main()
