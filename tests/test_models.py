import unittest
from datetime import datetime

from autoperf.models import MetricSample, RunStatus, utc_now


class ModelsTests(unittest.TestCase):
    def test_utc_now_is_parseable_iso_format(self):
        parsed = datetime.fromisoformat(utc_now())
        self.assertIsNotNone(parsed.tzinfo)

    def test_run_status_values_match_plain_strings(self):
        self.assertEqual(RunStatus.RUNNING, "running")
        self.assertEqual(RunStatus.COMPLETED, "completed")

    def test_metric_sample_default_labels_are_not_shared_between_instances(self):
        first = MetricSample("run", "cpu", "cpu.total", 1.0, "%")
        second = MetricSample("run", "cpu", "cpu.total", 2.0, "%")
        first.labels["key"] = "value"
        self.assertEqual(second.labels, {})


if __name__ == "__main__":
    unittest.main()
