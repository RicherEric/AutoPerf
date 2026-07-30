"""The one setUp every dashboard API test needs.

It was copy-pasted into four classes before, each with the same flaw: the
settings override was enabled in `setUp` and disabled in `tearDown`, so a
failure *inside* `setUp` left the override in place and leaked into every test
that ran after it. `enterContext` unwinds on the way out either way.

`SimpleTestCase` rather than `TestCase`: these views read AutoPerf's own SQLite
file, not the Django ORM, so there is no test database to wrap in a
transaction. The database under test is the temporary one below.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import Client, SimpleTestCase, override_settings

from autoperf.models import MetricSample
from autoperf.storage import BatchWriter


class ApiTestCase(SimpleTestCase):
    def setUp(self):
        from autoperf.storage import Storage

        tempdir = self.enterContext(TemporaryDirectory())
        self.db_path = Path(tempdir) / "test.db"
        self.storage = Storage(self.db_path)
        self.storage.initialize()
        self.recordings_root = Path(tempdir) / "recordings"
        self.enterContext(override_settings(
            AUTOPERF_DB_PATH=self.db_path, RECORDINGS_ROOT=self.recordings_root,
        ))
        self.client = Client()

    def _complete_run_with_samples(self, run_id, serial, values, scenario=None):
        """A finished run carrying cpu samples -- the shape stats and baselines read."""
        self.storage.create_run(run_id, serial, scenario)
        writer = BatchWriter(self.storage)
        writer.start()
        for value in values:
            writer.put(MetricSample(run_id, "cpu", "cpu.total", value, "%"))
        writer.close()
        self.storage.update_run(run_id, "completed")
