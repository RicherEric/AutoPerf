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


def _buckets(name, values, *, seconds_apart=60.0, start="2026-01-01T00:00:00+00:00"):
    from datetime import datetime, timedelta

    origin = datetime.fromisoformat(start)
    return [
        {
            "name": name,
            "bucket": index,
            "mean": value,
            "timestamp": (origin + timedelta(seconds=index * seconds_apart)).isoformat(),
        }
        for index, value in enumerate(values)
    ]


class ComputeTrendTests(unittest.TestCase):
    def test_detects_a_leak_that_the_mean_hides(self):
        """The case that motivates having a trend at all.

        Memory climbs steadily from 2.0 GB to 2.6 GB across the run. Its mean
        sits ~15% above the starting level -- under a conventional 20%
        regression threshold, so a mean-based comparison calls this a pass --
        while the device really is 30% worse off by the end.
        """
        from autoperf.analyzer import compare, compute_stats, compute_trend

        values = [2_000_000 + 600_000 * i / 99 for i in range(100)]
        # one hour of run time, sampled evenly
        trends = compute_trend(_buckets("memory.used", values, seconds_apart=3600 / 99))

        trend = trends["memory.used"]
        self.assertAlmostEqual(trend.span_hours, 1.0, places=3)
        self.assertGreater(trend.slope_per_hour, 500_000)
        self.assertGreater(trend.drift_pct, 20)

        # ...whereas the mean comparison this replaces does not flag it.
        healthy = compute_stats([{"name": "memory.used", "value": 2_000_000.0}])
        observed = compute_stats([{"name": "memory.used", "value": v} for v in values])
        self.assertFalse(compare(healthy, observed, threshold_pct=20.0)[0].regressed)

    def test_flat_metric_has_no_slope_or_drift(self):
        from autoperf.analyzer import compute_trend

        trends = compute_trend(_buckets("cpu.total", [40.0] * 50))
        self.assertAlmostEqual(trends["cpu.total"].slope_per_hour, 0.0, places=6)
        self.assertAlmostEqual(trends["cpu.total"].drift_pct, 0.0, places=6)

    def test_declining_metric_reports_negative_slope(self):
        from autoperf.analyzer import compute_trend

        trends = compute_trend(_buckets("battery.level", [100 - i for i in range(60)]))
        self.assertLess(trends["battery.level"].slope_per_hour, 0)
        self.assertLess(trends["battery.level"].drift_pct, 0)

    def test_single_bucket_has_zero_span_and_slope(self):
        from autoperf.analyzer import compute_trend

        trends = compute_trend(_buckets("cpu.total", [42.0]))
        trend = trends["cpu.total"]
        self.assertEqual(trend.span_hours, 0.0)
        self.assertEqual(trend.slope_per_hour, 0.0)
        self.assertEqual(trend.start_mean, 42.0)
        self.assertEqual(trend.end_mean, 42.0)

    def test_drift_is_none_when_the_run_starts_at_zero(self):
        from autoperf.analyzer import compute_trend

        trends = compute_trend(_buckets("cpu.total", [0.0] * 10 + [5.0] * 10))
        self.assertIsNone(trends["cpu.total"].drift_pct)

    def test_step_change_is_reported_by_drift_even_though_slope_understates_it(self):
        # A metric that jumps once and plateaus: a fitted line reports a
        # gentle continuous climb that never happened, so drift is what
        # carries the real magnitude of the move.
        from autoperf.analyzer import compute_trend

        trends = compute_trend(_buckets("memory.used", [100.0] * 50 + [200.0] * 50))
        self.assertAlmostEqual(trends["memory.used"].drift_pct, 100.0, places=6)

    def test_handles_multiple_metrics_independently(self):
        from autoperf.analyzer import compute_trend

        rows = _buckets("cpu.total", [40.0] * 20) + _buckets("memory.used", [100.0 + i for i in range(20)])
        trends = compute_trend(rows)
        self.assertEqual(set(trends), {"cpu.total", "memory.used"})
        self.assertAlmostEqual(trends["cpu.total"].slope_per_hour, 0.0, places=6)
        self.assertGreater(trends["memory.used"].slope_per_hour, 0)


class AppVersionDeltaTests(unittest.TestCase):
    """Whether two runs measured the same build of the app under test.

    Stated as its own field because it changes what a comparison *means*: if
    the app updated in between, the delta describes the app's change, not the
    device's -- and that is the likelier explanation of a sudden shift.
    """

    def _run(self, name, code):
        return {"app_package": "com.google.android.youtube",
                "app_version_name": name, "app_version_code": code}

    def test_same_build_is_not_a_change(self):
        from autoperf.analyzer import app_version_delta

        delta = app_version_delta(self._run("19.09.37", 1543), self._run("19.09.37", 1543))
        self.assertIs(delta["changed"], False)

    def test_a_different_build_is_reported_as_changed(self):
        from autoperf.analyzer import app_version_delta

        delta = app_version_delta(self._run("19.09.37", 1543), self._run("19.16.39", 1560))
        self.assertIs(delta["changed"], True)
        self.assertEqual(delta["baseline"]["version_name"], "19.09.37")
        self.assertEqual(delta["candidate"]["version_name"], "19.16.39")

    def test_a_version_code_bump_alone_still_counts(self):
        from autoperf.analyzer import app_version_delta

        self.assertIs(app_version_delta(self._run("19.09.37", 1543),
                                        self._run("19.09.37", 1544))["changed"], True)

    def test_unknown_is_not_reported_as_unchanged(self):
        # Runs predating version capture, or a package whose version could
        # not be read. Showing those as "same version" would assert something
        # that was never checked.
        from autoperf.analyzer import app_version_delta

        for baseline, candidate in (
            (None, self._run("19.09.37", 1543)),
            (self._run("19.09.37", 1543), None),
            ({}, {}),
            (self._run(None, None), self._run("19.09.37", 1543)),
        ):
            with self.subTest(baseline=baseline, candidate=candidate):
                self.assertIsNone(app_version_delta(baseline, candidate)["changed"])


class StatsFromAggregatesTests(unittest.TestCase):
    def test_builds_stats_from_sql_rows(self):
        from autoperf.analyzer import stats_from_aggregates

        stats = stats_from_aggregates([
            {"name": "cpu.total", "count": 4, "mean": 25.0, "variance": 125.0,
             "minimum": 10.0, "maximum": 40.0},
        ])
        self.assertEqual(stats["cpu.total"].count, 4)
        self.assertAlmostEqual(stats["cpu.total"].stdev, 125.0 ** 0.5, places=9)

    def test_treats_null_variance_as_zero(self):
        from autoperf.analyzer import stats_from_aggregates

        stats = stats_from_aggregates([
            {"name": "battery.level", "count": 1, "mean": 80.0, "variance": None,
             "minimum": 80.0, "maximum": 80.0},
        ])
        self.assertEqual(stats["battery.level"].stdev, 0.0)


if __name__ == "__main__":
    unittest.main()
