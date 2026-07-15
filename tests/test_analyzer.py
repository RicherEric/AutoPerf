import unittest

from autoperf.analyzer import compare, compute_stats


def _sample(name, value):
    return {"name": name, "value": value}


class ComputeStatsTests(unittest.TestCase):
    def test_groups_by_name_and_computes_mean_min_max(self):
        samples = [_sample("cpu.total", 10.0), _sample("cpu.total", 20.0), _sample("battery.level", 80.0)]
        stats = compute_stats(samples)
        self.assertEqual(stats["cpu.total"].count, 2)
        self.assertEqual(stats["cpu.total"].mean, 15.0)
        self.assertEqual(stats["cpu.total"].minimum, 10.0)
        self.assertEqual(stats["cpu.total"].maximum, 20.0)
        self.assertEqual(stats["battery.level"].mean, 80.0)
        self.assertEqual(stats["battery.level"].stdev, 0.0)

    def test_empty_samples_returns_empty_stats(self):
        self.assertEqual(compute_stats([]), {})


class CompareTests(unittest.TestCase):
    def test_flags_metric_beyond_threshold_as_regressed(self):
        baseline = compute_stats([_sample("cpu.total", 10.0)])
        candidate = compute_stats([_sample("cpu.total", 15.0)])
        results = compare(baseline, candidate, threshold_pct=20.0)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].regressed)
        self.assertAlmostEqual(results[0].delta_pct, 50.0)

    def test_does_not_flag_metric_within_threshold(self):
        baseline = compute_stats([_sample("cpu.total", 10.0)])
        candidate = compute_stats([_sample("cpu.total", 11.0)])
        results = compare(baseline, candidate, threshold_pct=20.0)
        self.assertFalse(results[0].regressed)

    def test_ignores_metrics_missing_from_candidate(self):
        baseline = compute_stats([_sample("cpu.total", 10.0), _sample("battery.level", 80.0)])
        candidate = compute_stats([_sample("cpu.total", 10.0)])
        results = compare(baseline, candidate)
        self.assertEqual([r.name for r in results], ["cpu.total"])

    def test_zero_baseline_mean_with_nonzero_candidate_is_regressed_with_no_delta_pct(self):
        baseline = compute_stats([_sample("custom.metric", 0.0)])
        candidate = compute_stats([_sample("custom.metric", 5.0)])
        results = compare(baseline, candidate)
        self.assertTrue(results[0].regressed)
        self.assertIsNone(results[0].delta_pct)

    def test_zero_baseline_and_zero_candidate_is_not_regressed(self):
        baseline = compute_stats([_sample("custom.metric", 0.0)])
        candidate = compute_stats([_sample("custom.metric", 0.0)])
        results = compare(baseline, candidate)
        self.assertFalse(results[0].regressed)


if __name__ == "__main__":
    unittest.main()
