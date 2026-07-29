import unittest
from unittest.mock import patch

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
        # Two reads now, not one: the size lines never change with rotation,
        # so the current rotation has to be asked for separately. See
        # ScreenSizeTests for what that is guarding against.
        self.assertEqual(adb.calls, [("device", "wm size", 10),
                                     ("device", "dumpsys window displays", 10)])

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


class ScreenSizeTests(unittest.TestCase):
    """`wm size` alone does not describe where a tap lands."""

    class Stub:
        def __init__(self, size, rotation="mCurrentRotation=0", fail_rotation=False):
            self.size, self.rotation, self.fail_rotation = size, rotation, fail_rotation

        def shell(self, serial, command, timeout=10):
            if command == "wm size":
                return self.size
            if "displays" in command:
                if self.fail_rotation:
                    raise RuntimeError("device offline")
                return self.rotation
            return ""

    def _size(self, **kwargs):
        return AndroidAdapter().screen_size(self.Stub(**kwargs), "S1")

    def test_portrait_is_reported_as_measured(self):
        self.assertEqual(self._size(size="Physical size: 1080x2340\n"), (1080, 2340))

    def test_landscape_swaps_the_dimensions(self):
        """Neither `wm size` line changes with rotation.

        A landscape tablet would otherwise have every fractional coordinate
        computed against portrait dimensions -- a tap meant for the top-right
        corner lands mid-left instead.
        """
        for rotation in ("mCurrentRotation=1", "mCurrentRotation=3"):
            with self.subTest(rotation=rotation):
                self.assertEqual(
                    self._size(size="Physical size: 1600x2560\n", rotation=rotation),
                    (2560, 1600),
                )

    def test_upside_down_is_still_portrait(self):
        self.assertEqual(
            self._size(size="Physical size: 1080x2340\n", rotation="mCurrentRotation=2"),
            (1080, 2340),
        )

    def test_a_display_size_override_supersedes_the_panel(self):
        # Input lands in the override, not in the physical panel size.
        self.assertEqual(
            self._size(size="Physical size: 1440x3200\nOverride size: 1080x2400\n"),
            (1080, 2400),
        )

    def test_an_override_is_rotated_too(self):
        self.assertEqual(
            self._size(size="Physical size: 1440x3200\nOverride size: 1080x2400\n",
                       rotation="mCurrentRotation=1"),
            (2400, 1080),
        )

    def test_an_unreadable_rotation_assumes_portrait(self):
        # Matches how every device behaved before rotation was considered at
        # all, so a failure here cannot make things worse than they were.
        self.assertEqual(self._size(size="Physical size: 1080x2340\n", rotation="nothing"),
                         (1080, 2340))
        self.assertEqual(self._size(size="Physical size: 1080x2340\n", fail_rotation=True),
                         (1080, 2340))

    def test_unparseable_size_still_raises(self):
        with self.assertRaises(ValueError):
            self._size(size="no size here")


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


HIERARCHY = (
    '<hierarchy>'
    '<node class="android.widget.ImageView" content-desc="Search" clickable="true" bounds="[940,80][1040,180]"/>'
    '<node class="android.widget.Button" content-desc="Shorts" clickable="true" focused="true" bounds="[600,2250][700,2320]"/>'
    '</hierarchy>'
)


class ElementAdbStub:
    """Answers the shell commands the element/verification actions issue."""

    def __init__(self, *, hierarchy=HIERARCHY, focus_package="com.google.android.youtube",
                 playback_state=3, dump_fails=False):
        self.hierarchy = hierarchy
        self.focus_package = focus_package
        self.playback_state = playback_state
        self.dump_fails = dump_fails
        self.commands = []

    def shell(self, serial, command, timeout=10):
        self.commands.append(command)
        if command.startswith("uiautomator"):
            if self.dump_fails:
                raise RuntimeError("could not get idle state")
            return "UI hierchary dumped to: /sdcard/window_dump.xml"
        if command.startswith("cat "):
            return self.hierarchy
        if command == "wm size":
            return "Physical size: 1080x2340\n"
        if command == "dumpsys window":
            if self.focus_package is None:
                return "nothing useful"
            return f"  mCurrentFocus=Window{{a b {self.focus_package}/{self.focus_package}.Main}}"
        if command == "dumpsys media_session":
            if self.playback_state is None:
                return ""
            return f"package=com.google.android.youtube\n state=PlaybackState {{state={self.playback_state}}}"
        return ""


class TapElementTests(unittest.TestCase):
    def setUp(self):
        # A stub device never changes between attempts, so the retry delay
        # only buys wall-clock. The production default is exercised on
        # hardware, not here.
        from autoperf.adapters import ElementActionsMixin
        patcher = patch.object(ElementActionsMixin, "RESOLVE_RETRY_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _target(self, **kwargs):
        from autoperf.uiauto import Selector, Target

        return Target(
            selectors=kwargs.pop("selectors", (Selector(content_desc="Search"),)),
            fallback=kwargs.pop("fallback", (0.92, 0.06)),
            name="search_icon",
        )

    def test_taps_the_located_element_and_reports_the_strategy(self):
        from autoperf.adapters import AndroidAdapter

        adb = ElementAdbStub()
        result = AndroidAdapter().tap_element(adb, "S1", self._target())
        self.assertEqual(result["strategy"], "content_desc")
        self.assertIn("input tap 990 130", adb.commands)

    def test_falls_back_to_coordinates_when_selectors_miss(self):
        from autoperf.adapters import AndroidAdapter
        from autoperf.uiauto import Selector

        adb = ElementAdbStub()
        result = AndroidAdapter().tap_element(
            adb, "S1", self._target(selectors=(Selector(resource_id="renamed"),))
        )
        # Still works -- exactly as well as before selectors existed -- but
        # says so, which is what makes the decay visible.
        self.assertEqual(result["strategy"], "coordinates")
        self.assertIn("input tap 994 140", adb.commands)

    def test_a_failed_ui_dump_degrades_to_coordinates_rather_than_failing(self):
        from autoperf.adapters import AndroidAdapter

        # uiautomator routinely refuses to dump while a video is playing
        # ("could not get idle state"), so this is a normal condition, not an
        # error worth ending a run over.
        adb = ElementAdbStub(dump_fails=True)
        result = AndroidAdapter().tap_element(adb, "S1", self._target())
        self.assertEqual(result["strategy"], "coordinates")

    def test_raises_when_nothing_matches_and_there_is_no_fallback(self):
        from autoperf.adapters import AndroidAdapter, ElementNotFound
        from autoperf.uiauto import Selector

        with self.assertRaises(ElementNotFound):
            AndroidAdapter().tap_element(
                ElementAdbStub(), "S1",
                self._target(selectors=(Selector(resource_id="gone"),), fallback=None),
            )


class VerificationActionTests(unittest.TestCase):
    def test_foreground_check_passes_for_the_expected_package(self):
        from autoperf.adapters import AndroidAdapter

        result = AndroidAdapter().verify_foreground(
            ElementAdbStub(), "S1", "com.google.android.youtube")
        self.assertTrue(result["verified"])

    def test_foreground_check_raises_when_another_app_is_in_front(self):
        from autoperf.adapters import AndroidAdapter, VerificationError

        adb = ElementAdbStub(focus_package="com.android.launcher")
        with self.assertRaises(VerificationError):
            AndroidAdapter().verify_foreground(adb, "S1", "com.google.android.youtube")

    def test_unreadable_focus_is_reported_as_unknown_not_as_a_failure(self):
        from autoperf.adapters import AndroidAdapter

        result = AndroidAdapter().verify_foreground(
            ElementAdbStub(focus_package=None), "S1", "com.google.android.youtube")
        self.assertIsNone(result["verified"])

    def test_playback_check_passes_while_playing(self):
        from autoperf.adapters import AndroidAdapter

        self.assertTrue(AndroidAdapter().verify_playing(ElementAdbStub(), "S1")["verified"])

    def test_playback_check_raises_when_nothing_is_playing(self):
        from autoperf.adapters import AndroidAdapter, VerificationError

        # The case a foreground check cannot catch: the app is in front, but
        # the taps that were supposed to start a video landed on nothing.
        with self.assertRaises(VerificationError):
            AndroidAdapter().verify_playing(ElementAdbStub(playback_state=2), "S1")

    def test_unknown_playback_state_is_not_treated_as_a_failure(self):
        from autoperf.adapters import AndroidAdapter

        self.assertIsNone(
            AndroidAdapter().verify_playing(ElementAdbStub(playback_state=None), "S1")["verified"])


class TvTapElementTests(unittest.TestCase):
    def _target(self, desc):
        from autoperf.uiauto import Selector, Target

        return Target(selectors=(Selector(content_desc=desc),), fallback=(0.5, 0.5), name=desc)

    def test_presses_select_when_the_target_is_already_focused(self):
        from autoperf.adapters import AndroidTvAdapter

        adb = ElementAdbStub()  # "Shorts" is the focused node in HIERARCHY
        result = AndroidTvAdapter().tap_element(adb, "S1", self._target("Shorts"))
        self.assertEqual(result["strategy"], "content_desc")
        self.assertIn("input keyevent KEYCODE_DPAD_CENTER", adb.commands)

    def test_walks_focus_toward_a_target_that_is_not_focused(self):
        from autoperf.adapters import AndroidTvAdapter

        # "Search" sits above the focused "Shorts" button, so focus must move
        # up rather than the remote blindly pressing select.
        adb = ElementAdbStub()
        try:
            AndroidTvAdapter().tap_element(adb, "S1", self._target("Search"))
        except Exception:
            pass  # focus never moves in a static stub; the keys are the point
        self.assertIn("input keyevent KEYCODE_DPAD_UP", adb.commands)
        self.assertNotIn("input keyevent KEYCODE_DPAD_CENTER", adb.commands)

    def test_gives_up_rather_than_pressing_select_blindly(self):
        from autoperf.adapters import AndroidTvAdapter, VerificationError

        with self.assertRaises(VerificationError):
            AndroidTvAdapter().tap_element(ElementAdbStub(), "S1", self._target("Search"))

    def test_verification_applies_the_same_package_mapping_as_launching(self):
        """Observed on a Chromecast: every TV run was marked unverified.

        Scenarios name the phone package and the TV adapter substitutes the
        `.tv` variant when launching, so comparing the scenario's package
        against what is actually in front failed unconditionally -- while the
        run itself was perfectly healthy.
        """
        from autoperf.adapters import AndroidTvAdapter

        adb = ElementAdbStub(focus_package="com.google.android.youtube.tv")
        result = AndroidTvAdapter().verify_foreground(adb, "S1", "com.google.android.youtube")
        self.assertTrue(result["verified"])

    def test_verification_still_rejects_a_genuinely_wrong_app(self):
        from autoperf.adapters import AndroidTvAdapter, VerificationError

        adb = ElementAdbStub(focus_package="com.netflix.ninja")
        with self.assertRaises(VerificationError):
            AndroidTvAdapter().verify_foreground(adb, "S1", "com.google.android.youtube")

    def test_mapped_package_leaves_unmapped_names_alone(self):
        from autoperf.adapters import AndroidTvAdapter

        adapter = AndroidTvAdapter()
        self.assertEqual(adapter.mapped_package("com.google.android.youtube"),
                         "com.google.android.youtube.tv")
        self.assertEqual(adapter.mapped_package("com.example.other"), "com.example.other")

    def test_never_falls_back_to_coordinates_on_a_tv(self):
        from autoperf.adapters import AndroidTvAdapter, ElementNotFound

        # A pixel is not something a remote control can address; pretending
        # otherwise is what produced meaningless TV runs.
        with self.assertRaises(ElementNotFound):
            AndroidTvAdapter().tap_element(ElementAdbStub(), "S1", self._target("NotPresent"))


if __name__ == "__main__":
    unittest.main()
