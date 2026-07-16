import unittest

from autoperf.scenarios import coords


class CoordsTests(unittest.TestCase):
    def test_rel_point_scales_by_screen_size(self):
        self.assertEqual(coords.rel_point((1000, 2000), 0.5, 0.25), (500, 500))

    def test_rel_point_rounds(self):
        self.assertEqual(coords.rel_point((1081, 2341), 0.33, 0.66), (357, 1545))

    def test_rel_tap_returns_tap_kwargs(self):
        self.assertEqual(coords.rel_tap((1080, 2340), 0.5, 0.5), {"x": 540, "y": 1170})

    def test_rel_swipe_returns_swipe_kwargs_with_default_duration(self):
        self.assertEqual(
            coords.rel_swipe((1000, 2000), 0.1, 0.1, 0.9, 0.9),
            {"x1": 100, "y1": 200, "x2": 900, "y2": 1800, "duration_ms": 300},
        )

    def test_rel_swipe_accepts_custom_duration(self):
        result = coords.rel_swipe((1000, 2000), 0.1, 0.1, 0.9, 0.9, duration_ms=900)
        self.assertEqual(result["duration_ms"], 900)

    def test_long_press_is_a_same_point_swipe(self):
        result = coords.long_press((1000, 2000), 0.5, 0.5, duration_ms=800)
        self.assertEqual((result["x1"], result["y1"]), (result["x2"], result["y2"]))
        self.assertEqual(result["duration_ms"], 800)

    def test_coordinates_scale_portably_across_different_screen_sizes(self):
        small = coords.rel_tap((720, 1560), 0.9, 0.08)
        large = coords.rel_tap((1440, 3120), 0.9, 0.08)
        self.assertEqual((large["x"], large["y"]), (small["x"] * 2, small["y"] * 2))


if __name__ == "__main__":
    unittest.main()
