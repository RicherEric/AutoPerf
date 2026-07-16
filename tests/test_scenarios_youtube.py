import unittest

from autoperf.scenarios import youtube

SCREEN = (1080, 2340)
KNOWN_ACTIONS = {"launch_app", "stop_app", "tap", "swipe", "key_event"}


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

    def test_every_preset_launches_youtube_first(self):
        for name in youtube.list_scenarios():
            with self.subTest(scenario=name):
                steps = youtube.build(name, SCREEN)
                self.assertEqual(steps[0].action, "launch_app")
                self.assertEqual(steps[0].kwargs["package"], youtube.PACKAGE)

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


if __name__ == "__main__":
    unittest.main()
