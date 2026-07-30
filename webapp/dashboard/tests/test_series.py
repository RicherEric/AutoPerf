"""The downsampled metric series the charts read.
"""

import json

from autoperf.models import MetricSample
from autoperf.storage import BatchWriter

from dashboard.tests.support import ApiTestCase


class RunSeriesApiTests(ApiTestCase):
    def test_series_caps_point_count_regardless_of_run_length(self):
        self.storage.create_run("r1", "S1")
        writer = BatchWriter(self.storage)
        writer.start()
        for i in range(1000):
            writer.put(MetricSample("r1", "cpu", "cpu.total", float(i), "%"))
        writer.close()

        response = self.client.get("/api/runs/r1/series?buckets=25")
        self.assertEqual(response.status_code, 200)
        series = response.json()["series"]
        self.assertEqual(len(series), 25)
        # The last bucket still carries the run's true maximum.
        self.assertEqual(max(point["maximum"] for point in series), 999.0)

    def test_series_of_an_empty_run_is_empty(self):
        self.storage.create_run("r1", "S1")
        self.assertEqual(self.client.get("/api/runs/r1/series").json()["series"], [])
