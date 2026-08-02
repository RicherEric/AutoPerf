"""Reproducing a decayed selector on purpose.

`autoperf.demo` exists so the framework's most important behaviour -- noticing
that an element was found by coordinate rather than by identity -- can be shown
without editing the selector table and restarting a worker. These tests pin the
two properties that make it safe to point at a real device: it changes only the
targets it was asked to change, and a name it does not recognise is an error
rather than a silent no-op.
"""

import unittest

from autoperf import demo, uiauto
from autoperf.adapters import ScenarioStep
from autoperf.scenarios import selectors, youtube

SCREEN = (1080, 2340)


def _targets(steps):
    return {step.kwargs["target"].name: step.kwargs["target"]
            for step in steps if step.kwargs.get("target") is not None}


class BlindTargetsTests(unittest.TestCase):
    def setUp(self):
        self.steps = youtube.build("search_and_play", SCREEN)

    def test_blinding_removes_only_the_named_target_s_selectors(self):
        blinded = _targets(demo.blind_targets(self.steps, ["search_icon"]))
        self.assertEqual(blinded["search_icon"].selectors, ())
        # Every other target in the same scenario is untouched.
        self.assertTrue(blinded["search_bar"].selectors)

    def test_blinding_keeps_the_coordinate_fallback(self):
        """Without a coordinate the target reports MISSING -- a different story.

        The point being demonstrated is "found, but only by coordinate", not
        "could not be found at all". `home_feed_video` is what the demo script
        actually blinds, and it is one of the targets that still carries a
        coordinate.
        """
        steps = youtube.build("home_feed_tap_video", SCREEN)
        blinded = _targets(demo.blind_targets(steps, ["home_feed_video"]))
        self.assertEqual(blinded["home_feed_video"].fallback,
                         selectors.HOME_FEED_VIDEO.fallback)

    def test_a_blinded_target_resolves_by_coordinates(self):
        """The behaviour the whole module exists to produce."""
        steps = youtube.build("home_feed_tap_video", SCREEN)
        blinded = _targets(demo.blind_targets(steps, ["home_feed_video"]))["home_feed_video"]
        nodes = uiauto.parse_hierarchy(
            '<hierarchy><node clickable="true" bounds="[0,200][1080,800]" '
            'class="android.view.ViewGroup"/></hierarchy>')
        # The node it would normally match is on screen and still ignored.
        self.assertEqual(
            uiauto.resolve(selectors.HOME_FEED_VIDEO, nodes, SCREEN).strategy, "structural")
        self.assertEqual(uiauto.resolve(blinded, nodes, SCREEN).strategy, "coordinates")

    def test_blinding_a_target_that_gave_up_its_coordinate_is_unreachable(self):
        """The other half of the same demo, now that ten targets carry no
        coordinate: blinding one of those does not degrade it to a pixel, it
        makes it unfindable -- which is exactly what dropping the fallback was
        for. `resolve` says None and the step fails instead of tapping a
        remembered spot and reporting success.
        """
        blinded = _targets(demo.blind_targets(self.steps, ["search_icon"]))["search_icon"]
        self.assertIsNone(blinded.fallback)
        nodes = uiauto.parse_hierarchy(
            '<hierarchy><node content-desc="Search" clickable="true" '
            'bounds="[0,0][100,100]" class="android.widget.ImageView"/></hierarchy>')
        self.assertEqual(
            uiauto.resolve(selectors.SEARCH_ICON, nodes, SCREEN).strategy, "content_desc")
        self.assertIsNone(uiauto.resolve(blinded, nodes, SCREEN))

    def test_the_original_steps_are_not_mutated(self):
        demo.blind_targets(self.steps, ["search_icon"])
        self.assertTrue(_targets(self.steps)["search_icon"].selectors)

    def test_no_names_returns_the_steps_unchanged(self):
        for empty in (None, []):
            self.assertIs(demo.blind_targets(self.steps, empty), self.steps)

    def test_an_unknown_name_raises_rather_than_blinding_nothing(self):
        with self.assertRaises(ValueError) as caught:
            demo.blind_targets(self.steps, ["serach_icon"])
        self.assertIn("serach_icon", str(caught.exception))

    def test_steps_without_a_target_pass_through(self):
        steps = [ScenarioStep(0.0, "launch_app", {"package": youtube.PACKAGE})]
        self.assertEqual(demo.blind_targets(steps, ["search_icon"]), steps)

    def test_known_target_names_covers_the_whole_selector_table(self):
        self.assertEqual(demo.known_target_names(),
                         sorted(target.name for target in selectors.ALL_TARGETS))

    def test_every_scenario_can_be_blinded_without_error(self):
        """A target named in a scenario but absent from the table would raise."""
        for name in youtube.list_scenarios():
            steps = youtube.build(name, SCREEN)
            present = sorted(_targets(steps))
            demo.blind_targets(steps, present)


if __name__ == "__main__":
    unittest.main()
