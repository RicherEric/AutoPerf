import unittest
from unittest.mock import patch

from autoperf.adapters import HOME, AndroidAdapter, AndroidTvAdapter, VerificationError
from tests.support import PAUSED, TOGGLES, DeviceAdb, NoWaits, RecordingAdb


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


class TypeTextTests(unittest.TestCase):
    def test_escapes_spaces_so_the_whole_phrase_arrives(self):
        # An unescaped space ends the argument, so only the first word would
        # be typed and the search would be for something else entirely.
        adb = RecordingAdb()
        AndroidAdapter().type_text(adb, "device", "lofi hip hop radio")
        self.assertEqual(adb.calls, [("device", "input text lofi%ship%shop%sradio", 10)])

    def test_rejects_shell_metacharacters(self):
        adb = RecordingAdb()
        for hostile in ("a; reboot", "a && rm -rf /", "$(id)", "a`id`", "a|b"):
            with self.subTest(text=hostile):
                with self.assertRaises(ValueError):
                    AndroidAdapter().type_text(adb, "device", hostile)
        self.assertEqual(adb.calls, [])

    def test_rejects_non_ascii(self):
        # `input text` is an ASCII keystroke injector: a Chinese query would
        # silently type nothing rather than fail, which is worse.
        with self.assertRaises(ValueError):
            AndroidAdapter().type_text(RecordingAdb(), "device", "搜尋關鍵字")


class WaitSurfaceTests(unittest.TestCase):
    def test_every_device_wait_is_listed_in_the_shared_helper(self):
        """A wait the helper does not know about is a wait a test will sit through.

        Three tests waiting out PLAYBACK_TIMEOUT once cost 24 of the suite's
        87 seconds, and nothing pointed at them. Adding a tunable to the mixin
        now fails here until it is either listed or given an injectable
        `timeout=`.
        """
        from autoperf.adapters import ElementActionsMixin
        from tests.support import WAIT_ATTRIBUTES

        waits = {name for name in vars(ElementActionsMixin)
                 if name.endswith(("_TIMEOUT", "_DELAY"))}
        self.assertEqual(waits, set(WAIT_ATTRIBUTES),
                         "unlisted device wait; add it to tests.support.WAIT_ATTRIBUTES")


class VerifyPlayingWaitTests(unittest.TestCase):
    """Playback starts asynchronously, so the check waits rather than samples.

    Deliberately does *not* use NoWaits: the waiting is the behaviour under
    test. Only the poll delay is zeroed, and each case caps its own patience
    with `timeout=`.
    """

    def setUp(self):
        patcher = patch.object(AndroidAdapter, "PLAYBACK_POLL_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _adb(states):
        return DeviceAdb(playback_states=states)

    def test_waits_through_buffering_before_succeeding(self):
        # Measured on a Galaxy A55: a flow that had genuinely reached the
        # right video still read as not-playing two seconds after the tap,
        # because a livestream was still buffering.
        adb = self._adb([6, 6, 6, 3])
        self.assertEqual(AndroidAdapter().verify_playing(adb, "S1"), {"verified": True})
        self.assertEqual(adb.playback_reads, 4)

    def test_still_fails_when_playback_never_starts(self):
        adb = self._adb([2] * 50)
        with self.assertRaises(VerificationError):
            AndroidAdapter().verify_playing(adb, "S1", timeout=0.05)

    def test_an_unreadable_session_stays_unknown_rather_than_failing(self):
        adb = self._adb([None] * 50)
        self.assertEqual(AndroidAdapter().verify_playing(adb, "S1", timeout=0.05),
                         {"verified": None})

    def test_succeeds_immediately_when_already_playing(self):
        adb = self._adb([3])
        self.assertEqual(AndroidAdapter().verify_playing(adb, "S1"), {"verified": True})
        self.assertEqual(adb.playback_reads, 1)


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


class TapElementTests(NoWaits, unittest.TestCase):
    def _target(self, **kwargs):
        from autoperf.uiauto import Selector, Target

        return Target(
            selectors=kwargs.pop("selectors", (Selector(content_desc="Search"),)),
            fallback=kwargs.pop("fallback", (0.92, 0.06)),
            name="search_icon",
        )

    def test_taps_the_located_element_and_reports_the_strategy(self):
        from autoperf.adapters import AndroidAdapter

        adb = DeviceAdb()
        result = AndroidAdapter().tap_element(adb, "S1", self._target())
        self.assertEqual(result["strategy"], "content_desc")
        self.assertIn("input tap 990 130", adb.commands)

    def test_falls_back_to_coordinates_when_selectors_miss(self):
        from autoperf.adapters import AndroidAdapter
        from autoperf.uiauto import Selector

        adb = DeviceAdb()
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
        adb = DeviceAdb(dump_fails=True)
        result = AndroidAdapter().tap_element(adb, "S1", self._target())
        self.assertEqual(result["strategy"], "coordinates")

    def test_raises_when_nothing_matches_and_there_is_no_fallback(self):
        from autoperf.adapters import AndroidAdapter, ElementNotFound
        from autoperf.uiauto import Selector

        with self.assertRaises(ElementNotFound):
            AndroidAdapter().tap_element(
                DeviceAdb(), "S1",
                self._target(selectors=(Selector(resource_id="gone"),), fallback=None),
            )


class VerificationActionTests(unittest.TestCase):
    def test_foreground_check_passes_for_the_expected_package(self):
        from autoperf.adapters import AndroidAdapter

        result = AndroidAdapter().verify_foreground(
            DeviceAdb(), "S1", "com.google.android.youtube")
        self.assertTrue(result["verified"])

    def test_foreground_check_raises_when_another_app_is_in_front(self):
        from autoperf.adapters import AndroidAdapter, VerificationError

        adb = DeviceAdb(focus_package="com.android.launcher")
        with self.assertRaises(VerificationError):
            AndroidAdapter().verify_foreground(adb, "S1", "com.google.android.youtube")

    def test_unreadable_focus_is_reported_as_unknown_not_as_a_failure(self):
        from autoperf.adapters import AndroidAdapter

        result = AndroidAdapter().verify_foreground(
            DeviceAdb(focus_package=None), "S1", "com.google.android.youtube")
        self.assertIsNone(result["verified"])

    def test_playback_check_passes_while_playing(self):
        from autoperf.adapters import AndroidAdapter

        self.assertTrue(AndroidAdapter().verify_playing(DeviceAdb(), "S1")["verified"])

    # A stub's state never changes, so waiting out the production
    # PLAYBACK_TIMEOUT here buys nothing but wall-clock. The wait itself is
    # covered by VerifyPlayingWaitTests, which scripts a changing state.
    def test_playback_check_raises_when_nothing_is_playing(self):
        from autoperf.adapters import AndroidAdapter, VerificationError

        # The case a foreground check cannot catch: the app is in front, but
        # the taps that were supposed to start a video landed on nothing.
        with self.assertRaises(VerificationError):
            AndroidAdapter().verify_playing(DeviceAdb(playback_state=PAUSED), "S1", timeout=0.01)

    def test_unknown_playback_state_is_not_treated_as_a_failure(self):
        from autoperf.adapters import AndroidAdapter

        self.assertIsNone(AndroidAdapter().verify_playing(
            DeviceAdb(playback_state=None), "S1", timeout=0.01)["verified"])


class VerifyElementStateTests(NoWaits, unittest.TestCase):
    """The check that separates "the like button was tapped" from "it liked"."""

    def _target(self, desc, fallback=(0.5, 0.5)):
        from autoperf.uiauto import Selector, Target

        return Target(selectors=(Selector(content_desc=desc),), fallback=fallback, name=desc)

    def _adb(self):
        return DeviceAdb(hierarchy=TOGGLES)

    def test_passes_when_the_element_reports_the_expected_state(self):
        from autoperf.adapters import AndroidAdapter

        result = AndroidAdapter().verify_element_state(
            self._adb(), "S1", self._target("喜歡這部影片"), state="selected", expected=True, timeout=0)
        self.assertTrue(result["verified"])
        self.assertEqual(result["strategy"], "content_desc")

    def test_reads_checked_as_well_as_selected(self):
        # Android exposes two toggle flavours and apps pick between them, so
        # supporting only one would leave half the controls unassertable.
        from autoperf.adapters import AndroidAdapter

        result = AndroidAdapter().verify_element_state(
            self._adb(), "S1", self._target("Autoplay"), state="checked", expected=True, timeout=0)
        self.assertTrue(result["verified"])

    def test_raises_when_the_state_is_not_the_expected_one(self):
        """The whole point: the control was found, and the action did not land.

        A tap on a like button succeeds whether or not the like registered --
        this is the only step that can tell the difference.
        """
        from autoperf.adapters import AndroidAdapter, VerificationError

        with self.assertRaises(VerificationError) as caught:
            AndroidAdapter().verify_element_state(
                self._adb(), "S1", self._target("訂閱"), state="selected", expected=True, timeout=0)
        # The message has to name the target and both values, or the report
        # says nothing a reader can act on.
        self.assertIn("訂閱", str(caught.exception))
        self.assertIn("selected", str(caught.exception))

    def test_an_expected_false_state_is_assertable_too(self):
        from autoperf.adapters import AndroidAdapter

        result = AndroidAdapter().verify_element_state(
            self._adb(), "S1", self._target("訂閱"), state="selected", expected=False, timeout=0)
        self.assertTrue(result["verified"])

    def test_a_missed_selector_is_unreadable_not_a_failure(self):
        """A coordinate carries no state, so it must not be treated as one.

        Every other action here accepts the coordinate fallback, because
        tapping a remembered pixel is still a tap. Reading a pixel's
        `selected` attribute is not a thing that exists -- and reporting the
        default False as "not selected" would fail runs for a decayed
        selector, which is the failure mode this whole layer removes.
        """
        from autoperf.adapters import AndroidAdapter

        result = AndroidAdapter().verify_element_state(
            self._adb(), "S1", self._target("no such label"), state="selected", expected=True, timeout=0)
        self.assertIsNone(result["verified"])
        self.assertIn("not located", result["detail"])

    def test_a_failed_ui_dump_is_unreadable_rather_than_a_failure(self):
        # uiautomator routinely refuses to dump while a video plays.
        from autoperf.adapters import AndroidAdapter

        adb = DeviceAdb(hierarchy=TOGGLES, dump_fails=True)
        result = AndroidAdapter().verify_element_state(
            adb, "S1", self._target("喜歡這部影片"), state="selected", expected=True, timeout=0)
        self.assertIsNone(result["verified"])

    def test_rejects_a_state_no_node_attribute_backs(self):
        # Including `enabled` would look supported and be unwritable: `find`
        # already skips disabled nodes, so expected=False could never match.
        from autoperf.adapters import AndroidAdapter

        for state in ("enabled", "focused", "", "Selected"):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    AndroidAdapter().verify_element_state(
                        self._adb(), "S1", self._target("訂閱"), state=state, timeout=0)

    def test_the_tv_adapter_inherits_the_same_check(self):
        # Reading state needs no touchscreen, so unlike tap_element there is
        # nothing for the TV adapter to do differently.
        from autoperf.adapters import AndroidTvAdapter

        result = AndroidTvAdapter().verify_element_state(
            self._adb(), "S1", self._target("喜歡這部影片"), state="selected", expected=True, timeout=0)
        self.assertTrue(result["verified"])


class TvTapElementTests(unittest.TestCase):
    def _target(self, desc):
        from autoperf.uiauto import Selector, Target

        return Target(selectors=(Selector(content_desc=desc),), fallback=(0.5, 0.5), name=desc)

    def test_presses_select_when_the_target_is_already_focused(self):
        from autoperf.adapters import AndroidTvAdapter

        adb = DeviceAdb()  # "Shorts" is the focused node in HIERARCHY
        result = AndroidTvAdapter().tap_element(adb, "S1", self._target("Shorts"))
        self.assertEqual(result["strategy"], "content_desc")
        self.assertIn("input keyevent KEYCODE_DPAD_CENTER", adb.commands)

    def test_walks_focus_toward_a_target_that_is_not_focused(self):
        from autoperf.adapters import AndroidTvAdapter

        # "Search" sits above the focused "Shorts" button, so focus must move
        # up rather than the remote blindly pressing select.
        adb = DeviceAdb()
        try:
            AndroidTvAdapter().tap_element(adb, "S1", self._target("Search"))
        except Exception:
            pass  # focus never moves in a static stub; the keys are the point
        self.assertIn("input keyevent KEYCODE_DPAD_UP", adb.commands)
        self.assertNotIn("input keyevent KEYCODE_DPAD_CENTER", adb.commands)

    def test_gives_up_rather_than_pressing_select_blindly(self):
        from autoperf.adapters import AndroidTvAdapter, VerificationError

        with self.assertRaises(VerificationError):
            AndroidTvAdapter().tap_element(DeviceAdb(), "S1", self._target("Search"))

    def test_verification_applies_the_same_package_mapping_as_launching(self):
        """Observed on a Chromecast: every TV run was marked unverified.

        Scenarios name the phone package and the TV adapter substitutes the
        `.tv` variant when launching, so comparing the scenario's package
        against what is actually in front failed unconditionally -- while the
        run itself was perfectly healthy.
        """
        from autoperf.adapters import AndroidTvAdapter

        adb = DeviceAdb(focus_package="com.google.android.youtube.tv")
        result = AndroidTvAdapter().verify_foreground(adb, "S1", "com.google.android.youtube")
        self.assertTrue(result["verified"])

    def test_verification_still_rejects_a_genuinely_wrong_app(self):
        from autoperf.adapters import AndroidTvAdapter, VerificationError

        adb = DeviceAdb(focus_package="com.netflix.ninja")
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
            AndroidTvAdapter().tap_element(DeviceAdb(), "S1", self._target("NotPresent"))


if __name__ == "__main__":
    unittest.main()
