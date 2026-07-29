import unittest

from autoperf import uiauto
from autoperf.uiauto import Node, Selector, Target

from tests.support import HOME_FEED as HIERARCHY, SCREEN, TOGGLES


class ParseHierarchyTests(unittest.TestCase):
    def test_flattens_nested_nodes_with_their_attributes(self):
        nodes = uiauto.parse_hierarchy(HIERARCHY)
        # Asserted structurally rather than by count: a node count couples
        # every test in this class to the shared fixture, so adding a node for
        # one test breaks the others for no reason.
        self.assertIn("Video C", [n.content_desc for n in nodes])   # nested three deep
        search = next(n for n in nodes if n.content_desc == "Search")
        self.assertEqual(search.resource_id, "com.google.android.youtube:id/search_button")
        self.assertEqual(search.bounds, (940, 80, 1040, 180))
        self.assertTrue(search.clickable)
        self.assertEqual(search.center, (990, 130))

    def test_malformed_input_yields_no_nodes_instead_of_raising(self):
        # A truncated dump is a device hiccup, and the caller's coordinate
        # fallback is a better response than aborting an entire run.
        for bad in ("", "   ", "<hierarchy", "not xml at all", "<hierarchy></hierarchy>"):
            with self.subTest(payload=bad):
                self.assertEqual(uiauto.parse_hierarchy(bad), [])

    def test_missing_bounds_degrade_to_zero_rather_than_raising(self):
        nodes = uiauto.parse_hierarchy('<hierarchy><node class="X"/></hierarchy>')
        self.assertEqual(nodes[0].bounds, (0, 0, 0, 0))
        self.assertEqual(nodes[0].center, (0, 0))


class ToggleStateTests(unittest.TestCase):
    """What backs `verify_element_state`: the node's own reported state."""

    def setUp(self):
        self.nodes = uiauto.parse_hierarchy(TOGGLES)

    def test_parses_selected_and_checked(self):
        by_desc = {n.content_desc: n for n in self.nodes}
        self.assertTrue(by_desc["喜歡這部影片"].selected)
        self.assertTrue(by_desc["Autoplay"].checked)

    def test_an_absent_attribute_reads_as_false_not_as_unknown(self):
        # A node that never mentions the attribute is the common case; the
        # "couldn't tell" distinction lives in the adapter, which knows
        # whether the node was found at all.
        by_desc = {n.content_desc: n for n in self.nodes}
        self.assertFalse(by_desc["訂閱"].selected)
        self.assertFalse(by_desc["喜歡這部影片"].checked)

    def test_describe_clickables_reports_toggle_state(self):
        # Whether a build exposes its toggle state at all can only be learned
        # from the device, and this is the report that answers it.
        described = uiauto.describe_clickables(self.nodes)
        by_desc = {d["content_desc"]: d for d in described}
        self.assertTrue(by_desc["喜歡這部影片"]["selected"])
        self.assertTrue(by_desc["Autoplay"]["checked"])
        self.assertFalse(by_desc["訂閱"]["selected"])

    def test_verifiable_states_excludes_states_that_cannot_be_asserted(self):
        # `enabled` is filtered out by `find` before an assertion could ever
        # see it, so offering it would be offering something unwritable.
        self.assertEqual(uiauto.VERIFIABLE_STATES, ("selected", "checked"))
        self.assertTrue(all(hasattr(Node(), state) for state in uiauto.VERIFIABLE_STATES))


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.nodes = uiauto.parse_hierarchy(HIERARCHY)

    def test_matches_by_content_desc(self):
        self.assertEqual(uiauto.find(self.nodes, Selector(content_desc="Search")).center, (990, 130))

    def test_bare_resource_id_matches_a_fully_qualified_one(self):
        # Which form a dump reports varies by Android version, so a selector
        # written either way has to work.
        self.assertIsNotNone(uiauto.find(self.nodes, Selector(resource_id="search_button")))
        self.assertIsNotNone(uiauto.find(
            self.nodes, Selector(resource_id="com.google.android.youtube:id/search_button")))

    def test_index_selects_in_document_order(self):
        node = uiauto.find(self.nodes, Selector(class_name="ViewGroup", clickable=True, index=2))
        self.assertEqual(node.content_desc, "Video C")

    def test_index_beyond_the_match_count_finds_nothing(self):
        self.assertIsNone(uiauto.find(self.nodes, Selector(class_name="ViewGroup", index=99)))

    def test_disabled_nodes_are_never_matched(self):
        self.assertIsNone(uiauto.find(self.nodes, Selector(text="Like")))

    def test_without_an_index_the_smallest_match_wins(self):
        # Containers nest around their buttons and match the same criteria;
        # the smallest match is the specific one, and a big container's
        # centre is usually padding.
        nodes = uiauto.parse_hierarchy(
            '<hierarchy>'
            '<node class="A" content-desc="x" bounds="[0,0][1000,1000]"/>'
            '<node class="A" content-desc="x" bounds="[10,10][60,60]"/>'
            '</hierarchy>'
        )
        self.assertEqual(uiauto.find(nodes, Selector(content_desc="x")).bounds, (10, 10, 60, 60))

    def test_min_area_filters_out_small_chrome(self):
        selector = Selector(clickable=True, min_area=200_000)
        matches = [n for n in self.nodes if selector.matches(n)]
        self.assertTrue(matches)
        self.assertTrue(all(n.area >= 200_000 for n in matches))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.nodes = uiauto.parse_hierarchy(HIERARCHY)

    def test_uses_the_first_selector_that_matches(self):
        target = Target((Selector(resource_id="gone"), Selector(content_desc="Search")), (0.5, 0.5), "s")
        resolution = uiauto.resolve(target, self.nodes, SCREEN)
        self.assertEqual(resolution.strategy, "content_desc")
        self.assertEqual(resolution.point, (990, 130))
        self.assertEqual(resolution.selector_index, 1)

    def test_falls_back_to_coordinates_when_every_selector_misses(self):
        # The decayed-selector case. It still works -- exactly as well as
        # before selectors existed -- but the strategy says so, which is what
        # makes the decay visible instead of silent.
        target = Target((Selector(resource_id="renamed_in_a_later_version"),), (0.92, 0.06), "search")
        resolution = uiauto.resolve(target, self.nodes, SCREEN)
        self.assertEqual(resolution.strategy, "coordinates")
        self.assertEqual(resolution.point, (994, 140))

    def test_returns_nothing_when_there_is_no_fallback_either(self):
        target = Target((Selector(resource_id="gone"),), None, "s")
        self.assertIsNone(uiauto.resolve(target, self.nodes, SCREEN))

    def test_an_empty_hierarchy_still_reaches_the_fallback(self):
        target = Target((Selector(content_desc="Search"),), (0.5, 0.5), "s")
        resolution = uiauto.resolve(target, [], SCREEN)
        self.assertEqual(resolution.strategy, "coordinates")


class FocusAndPlaybackTests(unittest.TestCase):
    class FakeAdb:
        def __init__(self, responses):
            self.responses = responses

        def shell(self, serial, command, timeout=10):
            for key, value in self.responses.items():
                if key in command:
                    if isinstance(value, Exception):
                        raise value
                    return value
            return ""

    def test_reads_current_focus_from_dumpsys(self):
        adb = self.FakeAdb({"dumpsys window":
                            "  mCurrentFocus=Window{a b com.google.android.youtube/com.google.android.youtube.HomeActivity}"})
        self.assertEqual(uiauto.current_focus(adb, "S1"),
                         ("com.google.android.youtube", "com.google.android.youtube.HomeActivity"))

    def test_unreadable_focus_is_none_not_an_exception(self):
        self.assertIsNone(uiauto.current_focus(self.FakeAdb({"dumpsys window": RuntimeError("x")}), "S1"))
        self.assertIsNone(uiauto.current_focus(self.FakeAdb({"dumpsys window": "nothing useful"}), "S1"))

    def test_detects_active_playback(self):
        adb = self.FakeAdb({"media_session": "package=com.google.android.youtube\n state=PlaybackState {state=3, position=42}"})
        self.assertIs(uiauto.is_playing(adb, "S1"), True)

    def test_detects_paused_playback(self):
        adb = self.FakeAdb({"media_session": "package=com.google.android.youtube\n state=PlaybackState {state=2, position=0}"})
        self.assertIs(uiauto.is_playing(adb, "S1"), False)

    def test_reads_the_named_state_form_current_android_emits(self):
        """Verbatim from a Chromecast on Android 14.

        Only the bare-number form was handled at first, so is_playing()
        returned "unknown" on every current device -- which meant
        verify_playing could never fail, and the strongest check in the system
        quietly verified nothing.
        """
        adb = self.FakeAdb({"media_session":
                            "      package=com.google.android.youtube.tv\n"
                            "      state=PlaybackState {state=PLAYING(3), position=37, "
                            "buffered position=0, speed=1.0, updated=256403092, actions=382}"})
        self.assertIs(uiauto.is_playing(adb, "S1"), True)

    def test_reads_the_named_paused_state(self):
        adb = self.FakeAdb({"media_session":
                            "package=com.google.android.youtube\n"
                            " state=PlaybackState {state=PAUSED(2), position=37}"})
        self.assertIs(uiauto.is_playing(adb, "S1"), False)

    def test_a_stopped_session_for_another_app_does_not_count_as_playing(self):
        adb = self.FakeAdb({"media_session":
                            "package=com.google.android.bluetooth\n"
                            " state=PlaybackState {state=ERROR(7), position=0}"})
        self.assertIs(uiauto.is_playing(adb, "S1", "com.google.android.youtube"), False)

    def test_unknown_playback_state_is_none_not_false(self):
        # "couldn't tell" must be distinguishable from "not playing", or a
        # momentarily unavailable dumpsys would fail runs for the wrong reason.
        self.assertIsNone(uiauto.is_playing(self.FakeAdb({"media_session": ""}), "S1"))
        self.assertIsNone(uiauto.is_playing(self.FakeAdb({"media_session": RuntimeError("x")}), "S1"))

    def test_a_package_not_present_in_the_dump_is_not_playing(self):
        adb = self.FakeAdb({"media_session": "package=com.spotify.music\n state=PlaybackState {state=3}"})
        self.assertIs(uiauto.is_playing(adb, "S1", "com.google.android.youtube"), False)

    def test_reads_the_package_version(self):
        adb = self.FakeAdb({"dumpsys package": "  versionCode=1543012928 minSdk=26\n  versionName=19.09.37\n"})
        self.assertEqual(
            uiauto.package_version(adb, "S1", "com.google.android.youtube"),
            {"package": "com.google.android.youtube", "version_name": "19.09.37", "version_code": 1543012928},
        )

    def test_unreadable_package_version_is_none(self):
        self.assertIsNone(uiauto.package_version(self.FakeAdb({"dumpsys package": ""}), "S1", "x.y"))
        self.assertIsNone(uiauto.package_version(
            self.FakeAdb({"dumpsys package": RuntimeError("boom")}), "S1", "x.y"))


class DumpHierarchyTests(unittest.TestCase):
    class FakeAdb:
        def __init__(self, dump_out, cat_out):
            self.dump_out, self.cat_out = dump_out, cat_out

        def shell(self, serial, command, timeout=10):
            return self.dump_out if command.startswith("uiautomator") else self.cat_out

    def test_returns_the_hierarchy_xml(self):
        adb = self.FakeAdb("UI hierchary dumped to: /sdcard/window_dump.xml", HIERARCHY)
        self.assertIn("<hierarchy", uiauto.dump_hierarchy(adb, "S1"))

    def test_raises_when_the_device_produces_no_hierarchy(self):
        with self.assertRaises(uiauto.UiDumpError):
            uiauto.dump_hierarchy(self.FakeAdb("dumped ok", "nothing here"), "S1")

    def test_raises_on_a_dump_error(self):
        with self.assertRaises(uiauto.UiDumpError):
            uiauto.dump_hierarchy(self.FakeAdb("ERROR: could not get idle state.", ""), "S1")


class DescribeClickablesTests(unittest.TestCase):
    def test_lists_identifiable_elements_without_duplicates(self):
        described = uiauto.describe_clickables(uiauto.parse_hierarchy(HIERARCHY))
        self.assertIn("Search", [entry["content_desc"] for entry in described])
        # Deduplicated on the identity triple, not on any single field --
        # several distinct rows legitimately share an empty content-desc.
        identities = [(e["resource_id"], e["text"], e["content_desc"]) for e in described]
        self.assertEqual(len(identities), len(set(identities)))

    def test_respects_the_limit(self):
        self.assertLessEqual(len(uiauto.describe_clickables(uiauto.parse_hierarchy(HIERARCHY), limit=2)), 2)


if __name__ == "__main__":
    unittest.main()
