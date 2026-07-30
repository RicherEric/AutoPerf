"""Handing work to Celery, and what happens when a device is busy.

Split out of one 1120-line module; the shared setUp lives in support.ApiTestCase.
"""

import json
from unittest.mock import patch

from autoperf.storage import Storage
from dashboard.services import trigger_run
from dashboard.tasks import run_test_task

from dashboard.tests.support import ApiTestCase


class EnqueueApiTests(ApiTestCase):
    @patch("dashboard.services.run_test_task")
    def test_trigger_run_creates_pending_row_and_enqueues_celery_task(self, mock_task):
        run_id = trigger_run(self.storage, "S1", 30)
        self.assertEqual(self.storage.get_run(run_id)["status"], "pending")
        mock_task.apply_async.assert_called_once_with(
            args=[self.storage.path, "S1", 30, run_id, None], task_id=run_id
        )

    @patch("dashboard.services.run_test_task")
    def test_trigger_run_passes_youtube_scenario_through(self, mock_task):
        run_id = trigger_run(self.storage, "S1", 30, "cold_start")
        mock_task.apply_async.assert_called_once_with(
            args=[self.storage.path, "S1", 30, run_id, "cold_start"], task_id=run_id
        )

    @patch("dashboard.services.celery_app")
    def test_queue_status_reports_online_workers(self, mock_celery_app):
        inspector = mock_celery_app.control.inspect.return_value
        inspector.active.return_value = {"worker1@host": [{"id": "abc", "name": "dashboard.run_test"}]}
        inspector.reserved.return_value = {"worker1@host": []}
        inspector.scheduled.return_value = {}

        response = self.client.get("/api/queue")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["broker_reachable"])
        self.assertTrue(payload["worker_online"])
        self.assertEqual(payload["workers"], [
            {"name": "worker1@host", "active": [{"id": "abc", "name": "dashboard.run_test"}], "reserved": [], "scheduled": []}
        ])

    @patch("dashboard.services.celery_app")
    def test_queue_status_reports_no_worker_online_as_a_normal_state(self, mock_celery_app):
        inspector = mock_celery_app.control.inspect.return_value
        inspector.active.return_value = None
        inspector.reserved.return_value = None
        inspector.scheduled.return_value = None

        response = self.client.get("/api/queue")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["broker_reachable"])
        self.assertFalse(payload["worker_online"])
        self.assertEqual(payload["workers"], [])

    @patch("dashboard.services.celery_app")
    def test_queue_status_reports_broker_unreachable_distinctly(self, mock_celery_app):
        mock_celery_app.control.inspect.side_effect = OSError("connection refused")

        response = self.client.get("/api/queue")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["broker_reachable"])
        self.assertFalse(payload["worker_online"])
        self.assertIn("connection refused", payload["error"])

    @patch("dashboard.services.celery_app")
    def test_queue_status_reports_running_runs_from_storage_even_when_celery_sees_nothing(self, mock_celery_app):
        # This is the solo-pool blind spot: the worker is fully synchronous, so
        # it cannot answer an inspect() broadcast while busy executing a task
        # -- active/reserved/scheduled all come back empty even though a run
        # is genuinely in progress. running_runs must still show it, since it
        # reads Storage directly rather than going through Celery at all.
        inspector = mock_celery_app.control.inspect.return_value
        inspector.active.return_value = {}
        inspector.reserved.return_value = {}
        inspector.scheduled.return_value = {}

        self.storage.create_run("run1", "S1")
        self.storage.update_run("run1", "running", checkpoint="12.3")

        response = self.client.get("/api/queue")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["worker_online"])
        self.assertEqual([r["id"] for r in payload["running_runs"]], ["run1"])
