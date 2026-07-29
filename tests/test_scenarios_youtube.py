import unittest

from autoperf.scenarios import youtube

SCREEN = (1080, 2340)
KNOWN_ACTIONS = {
    "launch_app", "stop_app", "tap", "swipe", "key_event",
    "tap_element", "verify_foreground", "verify_playing",
}
VERIFY_ACTIONS = {"verify_foreground", "verify_playing"}


class YoutubeScenarioRegistryTests(unittest.TestCase):
    def test_list_scenarios_matches_registry_keys(self):
        self.assertEqual(youtube.list_scenarios(), sorted(youtube.REGISTRY))

    def test_registry_has_at_least_fifteen_presets(self):
        self.assertGreaterEqual(len(youtube.REGISTRY), 15)

    def test_every_preset_builds_a_nonempty_valid_step_list(self):
        for name in youtube.list_scenarios():
            with self.subTest(scenario=name):
                steps = youtube.build(name, SCREEN)
                self.assertTrue(steps)
                for step in steps:
                    self.assertIn(step.action, KNOWN_ACTIONS)
                    self.assertGreaterEqual(step.at, 0.0)

    def test_every_preset_launches_its_target_app_first(self):
        for name in youtube.list_scenarios():
            with self.subTest(scenario=name):
                steps = youtube.build(name, SCREEN)
                self.assertEqual(steps[0].action, "launch_app")
                expected = youtube.SETTINGS_PACKAGE if name == "device_settings_scroll" else youtube.PACKAGE
                self.assertEqual(steps[0].kwargs["package"], expected)

    def test_steps_within_a_preset_are_chronologically_ordered(self):
        for name in youtube.list_scenarios():
            with self.subTest(scenario=name):
                steps = youtube.build(name, SCREEN)
                self.assertEqual([s.at for s in steps], sorted(s.at for s in steps))

    def test_tap_and_swipe_coordinates_are_within_screen_bounds(self):
        width, height = SCREEN
        for name in youtube.list_scenarios():
            steps = youtube.build(name, SCREEN)
            for step in steps:
                if step.action == "tap":
                    self.assertTrue(0 <= step.kwargs["x"] <= width)
                    self.assertTrue(0 <= step.kwargs["y"] <= height)
                elif step.action == "swipe":
                    for key, bound in (("x1", width), ("x2", width), ("y1", height), ("y2", height)):
                        self.assertTrue(0 <= step.kwargs[key] <= bound)

    def test_no_preset_taps_a_bare_coordinate_any_more(self):
        """Coordinate taps must only ever be reached as a *fallback*.

        A raw `tap` step cannot fail: `adb shell input tap` succeeds on empty
        space, so a missed tap was recorded as a completed action and the run
        finished green having measured an untouched screen. Every tap is now
        a `tap_element`, which carries its coordinate as a last resort inside
        the Target and reports when it had to use it.
        """
        offenders = [
            name for name in youtube.list_scenarios()
            if any(step.action == "tap" for step in youtube.build(name, SCREEN))
        ]
        self.assertEqual(offenders, [])

    def test_every_tap_element_target_keeps_a_coordinate_fallback(self):
        # The selectors are unverified guesses until captured from a real
        # device; without the fallback a wrong guess would break a scenario
        # that previously worked.
        for name in youtube.list_scenarios():
            for step in youtube.build(name, SCREEN):
                if step.action == "tap_element":
                    target = step.kwargs["target"]
                    with self.subTest(scenario=name, target=target.name):
                        self.assertTrue(target.selectors, "target has no selectors at all")
                        self.assertIsNotNone(target.fallback)
                        self.assertTrue(all(0.0 <= f <= 1.0 for f in target.fallback))

    def test_scenarios_that_claim_playback_assert_it(self):
        """Anything whose description promises a playing video must check.

        This is the assertion that separates "the flow worked" from "four
        taps landed on empty space and the home feed is still showing" --
        a foreground check passes in both cases.
        """
        for name in ("search_and_play", "play_golden", "background_foreground_resume"):
            with self.subTest(scenario=name):
                actions = {step.action for step in youtube.build(name, SCREEN)}
                self.assertIn("verify_playing", actions)

    def test_every_preset_verifies_its_app_actually_came_to_the_front(self):
        for name in youtube.list_scenarios():
            with self.subTest(scenario=name):
                actions = {step.action for step in youtube.build(name, SCREEN)}
                self.assertTrue(actions & VERIFY_ACTIONS,
                                "preset performs no verification at all")

    def test_build_raises_on_unknown_scenario(self):
        with self.assertRaises(ValueError):
            youtube.build("not-a-real-scenario", SCREEN)

    def test_every_preset_has_a_valid_tier_and_nonempty_description(self):
        for preset in youtube.REGISTRY.values():
            with self.subTest(scenario=preset.name):
                self.assertIn(preset.tier, youtube.TIERS)
                self.assertTrue(preset.description)

    def test_list_scenarios_filters_by_tier(self):
        smoke_names = youtube.list_scenarios(tier=youtube.TIER_SMOKE)
        self.assertIn("cold_start", smoke_names)
        self.assertNotIn("multi_video_session", smoke_names)
        for name in smoke_names:
            self.assertEqual(youtube.REGISTRY[name].tier, youtube.TIER_SMOKE)

    def test_every_tier_has_at_least_one_scenario(self):
        for tier in youtube.TIERS:
            with self.subTest(tier=tier):
                self.assertTrue(youtube.list_scenarios(tier=tier))

    def test_describe_scenarios_returns_name_description_tier(self):
        descriptions = youtube.describe_scenarios()
        self.assertEqual(len(descriptions), len(youtube.REGISTRY))
        cold_start = next(d for d in descriptions if d["name"] == "cold_start")
        self.assertEqual(cold_start["tier"], youtube.TIER_SMOKE)
        self.assertTrue(cold_start["description"])

    def test_describe_scenarios_filters_by_tier(self):
        descriptions = youtube.describe_scenarios(tier=youtube.TIER_REGRESSION)
        self.assertTrue(descriptions)
        self.assertTrue(all(d["tier"] == youtube.TIER_REGRESSION for d in descriptions))

    def test_named_video_presets_deep_link_to_the_expected_video(self):
        for video in youtube.NAMED_VIDEOS:
            with self.subTest(video=video.key):
                name = f"play_{video.key}"
                self.assertIn(name, youtube.REGISTRY)
                steps = youtube.build(name, SCREEN)
                step = steps[0]
                self.assertEqual(step.action, "launch_app")
                self.assertEqual(step.kwargs["package"], youtube.PACKAGE)
                self.assertEqual(step.kwargs["data"], f"https://www.youtube.com/watch?v={video.video_id}")
                self.assertEqual(len(video.video_id), 11)
                # The deep link is the whole mechanism -- it must never be
                # joined by taps, which is what these presets exist to avoid.
                self.assertEqual([s.action for s in steps[1:]],
                                 ["verify_foreground", "verify_playing"])


if __name__ == "__main__":
    unittest.main()
