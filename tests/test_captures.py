"""The only tests here that check against what a device really said.

Every other fixture in this suite is a literal somebody typed, which makes it a
guess about a device they did not have in front of them. The cost of that is in
the git log: `verify_playing`'s parser matched only a format no modern device
emits, so the strongest check in the system verified nothing while 305 tests
stayed green. Nineteen selector labels were wrong. The search flow never typed
anything and seven scenarios passed anyway. Every one was found by running
against hardware; not one by a test.

These are characterization tests, pinned to a device and an app build recorded
in the manifest. **They are supposed to fail when YouTube updates** -- that is
the signal the selector table has expired, and it is the signal this suite did
not have. When one fails: re-capture, read the diff, fix `selectors.py`.

    autoperf capture --serial <SERIAL> --name home_feed --app com.google.android.youtube
"""

import unittest

from autoperf import uiauto
from autoperf.scenarios import selectors
from tests.support import (
    SCREEN, YOUTUBE, CapturedAdb, capture_dirs, capture_manifest, load_capture,
)

# Navigation is the stable core: these four exist on every YouTube screen that
# has a bottom bar, and they are what a scenario needs before it can reach
# anything else. Verified present in the home_feed, library and shorts captures.
NAVIGATION = ("search_icon", "shorts_tab", "subscriptions_tab", "library_tab")


def _targets() -> dict:
    return {t.name: t for t in vars(selectors).values() if isinstance(t, uiauto.Target)}


def _resolve(capture: str, target_name: str):
    nodes = uiauto.parse_hierarchy(load_capture(capture))
    return uiauto.resolve(_targets()[target_name], nodes, SCREEN)


class CaptureIntegrityTests(unittest.TestCase):
    """A capture of the wrong thing is worse than no capture: it looks like evidence."""

    def setUp(self):
        self.manifest = capture_manifest()["captures"]

    def test_there_are_captures_to_test_against(self):
        # Raising rather than skipping is the point: a validity layer that
        # skips itself is indistinguishable from one that passes.
        self.assertTrue(capture_dirs(), "no captures stored; run `autoperf capture`")
        self.assertTrue(self.manifest)

    def test_every_capture_records_where_it_came_from(self):
        """Without provenance a capture is just another literal of unknown origin.

        Which is the problem it exists to solve -- the labels in the selector
        table are only valid for one app build, and a fixture that cannot say
        which build it came from cannot say whether it has expired.
        """
        for name, entry in self.manifest.items():
            with self.subTest(capture=name):
                self.assertTrue(entry["device"]["model"])
                self.assertTrue(entry["device"]["android_release"])
                self.assertTrue(entry["captured_at"])
                self.assertGreater(entry["node_count"], 0)

    def test_every_capture_parses_to_the_node_count_it_claims(self):
        for name, entry in self.manifest.items():
            with self.subTest(capture=name):
                nodes = uiauto.parse_hierarchy(load_capture(name))
                self.assertEqual(len(nodes), entry["node_count"])

    def test_every_capture_is_of_the_screen_it_says_it_is(self):
        """The guard that caught a mislabelled capture.

        The first attempt at `shorts` tapped a guessed coordinate, missed the
        tab, and stored the launcher under the name `shorts`. Recording the
        focused package is what made that visible.
        """
        for name, entry in self.manifest.items():
            with self.subTest(capture=name):
                focused = (entry.get("focus") or {}).get("package")
                expected = "com.sec.android.app.launcher" if name == "launcher" else YOUTUBE
                self.assertEqual(focused, expected)

    def test_a_capture_spans_time_and_says_how_much(self):
        """A capture is a window, not an instant.

        The hierarchy dump takes seconds -- 11.7s on a playing watch page --
        while the dumpsys reads take about 120ms. Taken in the wrong order, the
        first watch-page capture stored a playing hierarchy beside a
        `media_session` that said STOPPED, because the 19-second video ended
        during the dump. The reads are now taken cheap-first, and the span is
        recorded so a reader comparing two files knows how far apart they were.
        """
        watch = self.manifest["watch_page_playing"]
        self.assertIn("capture_seconds", watch)
        self.assertGreater(watch["capture_seconds"], 1.0)


class ParserAgainstRealOutputTests(unittest.TestCase):
    """The two parsers that were wrong on hardware while their fixtures agreed."""

    def test_playback_is_detected_in_a_real_dumpsys_from_a_playing_video(self):
        """The regression that made the strongest check in the system verify nothing.

        `dumpsys media_session` reports `state=PLAYING(3)` on a modern device
        and a bare `state=3` on an older one. The parser handled only the bare
        form, so `is_playing` returned "unknown" on every current phone -- and
        an unknown never fails, so `verify_playing` passed no matter what the
        device was doing. A hand-written fixture cannot catch that. This can.
        """
        self.assertIs(
            uiauto.is_playing(CapturedAdb("watch_page_playing"), "S1", YOUTUBE), True)

    def test_a_screen_with_no_playback_reads_as_not_playing(self):
        self.assertIs(uiauto.is_playing(CapturedAdb("home_feed"), "S1", YOUTUBE), False)

    def test_the_focused_package_is_read_from_a_real_dumpsys_window(self):
        focus = uiauto.current_focus(CapturedAdb("home_feed"), "S1")
        self.assertIsNotNone(focus)
        self.assertEqual(focus[0], YOUTUBE)
        self.assertTrue(focus[1], "no activity parsed out of 69KB of real output")

    def test_a_real_hierarchy_flattens_and_keeps_its_attributes(self):
        nodes = uiauto.parse_hierarchy(load_capture("home_feed"))
        self.assertGreater(len(nodes), 50)
        clickable = [n for n in nodes if n.clickable]
        self.assertTrue(clickable)
        self.assertTrue(any(n.content_desc for n in clickable))
        self.assertTrue(any(n.resource_id for n in nodes))


class SelectorTableAgainstRealScreensTests(unittest.TestCase):
    """Does the table in selectors.py actually find things on this app build?

    Pinned to the build in the manifest. A failure here means the table has
    expired, not that the test is wrong -- re-capture and read the diff.
    """

    def test_navigation_resolves_by_selector_on_the_home_feed(self):
        for name in NAVIGATION:
            with self.subTest(target=name):
                resolution = _resolve("home_feed", name)
                self.assertIsNotNone(resolution)
                self.assertNotEqual(
                    resolution.strategy, "coordinates",
                    f"{name} no longer matches a real home feed; re-capture and fix selectors.py")

    def test_a_feed_row_is_found_structurally_not_by_label(self):
        # "Some video", which is the actual intent -- and it survives a rename
        # of anything, which a label does not.
        resolution = _resolve("home_feed", "home_feed_video")
        self.assertEqual(resolution.strategy, "structural")

    def test_both_like_controls_are_locatable_by_label(self):
        """The asymmetry this test used to assert is gone -- and that is the point.

        On 21.29.366 the Shorts like control matched by content-desc while the
        watch-page one was not in the hierarchy at all, so the table carried a
        coordinate for it. Re-capturing against 21.30.209 turned that assertion
        red: the watch-page control now carries its own label. The app became
        more introspectable, and the only reason we know is that the capture
        was retaken -- a frozen capture would have kept agreeing with itself.

        Findability is not the same question as `verify_element_state`, which
        still must not be pointed at either control: `selected`/`checked` do
        not move on a tap (see LIKE_BUTTON in scenarios/selectors.py).
        """
        self.assertEqual(_resolve("shorts", "shorts_like_button").strategy, "content_desc")
        self.assertEqual(_resolve("watch_page_playing", "like_button").strategy, "content_desc")

    def test_the_player_overlay_is_absent_from_a_real_watch_page(self):
        # The transport controls the player draws for itself are still not in
        # the tree, so these keep relying on their coordinate -- and preflight
        # reports them as falling back rather than pretending.
        for name in ("fullscreen_enter", "fullscreen_exit",
                     "quality_row", "quality_option", "pip_caret"):
            with self.subTest(target=name):
                self.assertEqual(_resolve("watch_page_playing", name).strategy, "coordinates")

    def test_the_player_container_is_addressable_even_though_its_controls_are_not(self):
        """`player_surface` sat in the list above until 21.30.209.

        It is the container the player draws *into*, not one of the drawn
        controls, and on this build it carries a resource-id. Split out so the
        next capture has to answer for it separately: "the overlay is absent"
        and "the surface is unaddressable" were being asserted as one claim,
        and only the first of them is still true.
        """
        self.assertEqual(_resolve("watch_page_playing", "player_surface").strategy, "resource_id")

    def test_every_target_either_matches_somewhere_or_has_a_coordinate(self):
        """No target may be unreachable on every screen we have evidence for.

        A target that matches nowhere and has no fallback would raise
        ElementNotFound mid-run; one with a fallback still works. This is the
        weaker, always-true half of the table's contract.
        """
        captures = [c for c in capture_manifest()["captures"] if c != "launcher"]
        for name, target in _targets().items():
            with self.subTest(target=name):
                matched = any(
                    (r := _resolve(capture, name)) is not None and r.strategy != "coordinates"
                    for capture in captures)
                self.assertTrue(matched or target.fallback is not None,
                                "matches on no captured screen and has no coordinate")


if __name__ == "__main__":
    unittest.main()
