import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoperf import cli
from autoperf.models import MetricSample
from autoperf.storage import BatchWriter, Storage
from tests.support import EMPTY_SCREEN, DeviceAdb, NoWaits


def FakeAdb():
    return DeviceAdb(metrics=True)


class CliTests(unittest.TestCase):
    def test_status_reports_missing_run_and_returns_error_code(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "status", "missing-run"])
            self.assertEqual(code, 1)
            self.assertIn("Run not found", out.getvalue())

    def test_devices_command_registers_and_prints_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with patch("autoperf.cli.AdbClient", return_value=FakeAdb()):
                with contextlib.redirect_stdout(out):
                    code = cli.main(["--db", str(db), "devices"])
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(out.getvalue()),
                [{"serial": "SERIAL1", "state": "device", "model": "Pixel"}],
            )
            storage = Storage(db)
            conn = storage.connect()
            try:
                row = conn.execute("SELECT model FROM devices WHERE serial=?", ("SERIAL1",)).fetchone()
            finally:
                conn.close()
            self.assertEqual(row, ("Pixel",))

    def test_run_command_executes_and_prints_completed_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with patch("autoperf.cli.AdbClient", return_value=FakeAdb()):
                with contextlib.redirect_stdout(out):
                    code = cli.main(["--db", str(db), "run", "--serial", "SERIAL1", "--duration", "0.05"])
            self.assertEqual(code, 0)
            run_id = out.getvalue().strip()
            storage = Storage(db)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")

    def test_run_command_with_app_flag_drives_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with patch("autoperf.cli.AdbClient", return_value=FakeAdb()):
                with contextlib.redirect_stdout(out):
                    code = cli.main([
                        "--db", str(db), "run", "--serial", "SERIAL1", "--duration", "0.05",
                        "--app", "com.example.app",
                    ])
            self.assertEqual(code, 0)
            run_id = out.getvalue().strip()
            storage = Storage(db)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")
            conn = storage.connect()
            try:
                kinds = {row[0] for row in conn.execute(
                    "SELECT kind FROM test_events WHERE run_id=?", (run_id,)
                )}
            finally:
                conn.close()
            self.assertIn("adapter_action", kinds)

    def _seed_run_with_samples(self, storage, run_id, serial, values):
        storage.create_run(run_id, serial)
        writer = BatchWriter(storage)
        writer.start()
        for value in values:
            writer.put(MetricSample(run_id, "cpu", "cpu.total", value, "%"))
        writer.close()

    def test_baseline_set_rejects_run_from_a_different_device(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            storage = Storage(db)
            storage.initialize()
            storage.create_run("run1", "OTHER_SERIAL")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "baseline", "set", "--serial", "SERIAL1", "--run", "run1"])
            self.assertEqual(code, 1)

    def test_baseline_set_and_show_returns_computed_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            storage = Storage(db)
            storage.initialize()
            self._seed_run_with_samples(storage, "run1", "SERIAL1", [10.0, 20.0])

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "baseline", "set", "--serial", "SERIAL1", "--run", "run1"])
            self.assertEqual(code, 0)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "baseline", "show", "--serial", "SERIAL1"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["run_id"], "run1")
            self.assertEqual(payload["stats"]["cpu.total"]["mean"], 15.0)

    def test_compare_without_baseline_returns_error(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            storage = Storage(db)
            storage.initialize()
            self._seed_run_with_samples(storage, "run1", "SERIAL1", [10.0])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "compare", "--run", "run1"])
            self.assertEqual(code, 1)
            self.assertIn("No baseline set", out.getvalue())

    def test_compare_flags_regression_against_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            storage = Storage(db)
            storage.initialize()
            self._seed_run_with_samples(storage, "baseline_run", "SERIAL1", [10.0])
            self._seed_run_with_samples(storage, "candidate_run", "SERIAL1", [50.0])
            storage.set_baseline("SERIAL1", "baseline_run")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "compare", "--run", "candidate_run"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["regressed"])
            self.assertEqual(payload["metrics"][0]["name"], "cpu.total")

    def test_youtube_scenarios_list_prints_name_description_tier(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "youtube-scenarios", "list"])
            self.assertEqual(code, 0)
            entries = json.loads(out.getvalue())
            self.assertGreaterEqual(len(entries), 15)
            cold_start = next(e for e in entries if e["name"] == "cold_start")
            self.assertEqual(cold_start["tier"], "smoke")
            self.assertTrue(cold_start["description"])

    def test_youtube_scenarios_list_filters_by_tier(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", str(db), "youtube-scenarios", "list", "--tier", "smoke"])
            self.assertEqual(code, 0)
            entries = json.loads(out.getvalue())
            self.assertTrue(entries)
            self.assertTrue(all(e["tier"] == "smoke" for e in entries))

    def test_run_command_with_youtube_scenario_drives_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with patch("autoperf.cli.AdbClient", return_value=FakeAdb()):
                with contextlib.redirect_stdout(out):
                    code = cli.main([
                        "--db", str(db), "run", "--serial", "SERIAL1", "--duration", "0.05",
                        "--youtube-scenario", "cold_start",
                    ])
            self.assertEqual(code, 0)
            run_id = out.getvalue().strip()
            storage = Storage(db)
            self.assertEqual(storage.get_run(run_id)["status"], "completed")
            conn = storage.connect()
            try:
                kinds = {row[0] for row in conn.execute(
                    "SELECT kind FROM test_events WHERE run_id=?", (run_id,)
                )}
            finally:
                conn.close()
            self.assertIn("adapter_action", kinds)
            self.assertEqual(storage.get_run(run_id)["youtube_scenario"], "cold_start")

    def test_run_command_rejects_app_and_youtube_scenario_together(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.main([
                        "--db", str(db), "run", "--serial", "SERIAL1",
                        "--app", "com.example.app", "--youtube-scenario", "cold_start",
                    ])

    def test_run_suite_command_runs_every_scenario_in_a_tier(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            out = io.StringIO()
            with patch("autoperf.cli.AdbClient", return_value=FakeAdb()):
                with contextlib.redirect_stdout(out):
                    code = cli.main([
                        "--db", str(db), "run-suite", "--serial", "SERIAL1",
                        "--tier", "smoke", "--duration", "0.05",
                    ])
            self.assertEqual(code, 0)
            results = json.loads(out.getvalue())
            self.assertEqual(
                sorted(r["scenario"] for r in results),
                ["cold_start", "cold_start_and_stop", "device_settings_scroll", "home_feed_scroll", "search_and_play"],
            )
            self.assertTrue(all(r["status"] == "completed" for r in results))

            storage = Storage(db)
            for result in results:
                self.assertEqual(storage.get_run(result["run_id"])["youtube_scenario"], result["scenario"])


class CampaignCliTests(unittest.TestCase):
    """The CLI must be able to drive a campaign with no Django, Celery or
    Redis present -- the framework-first property the architecture bible
    asks for, and the reason campaign logic lives in autoperf.campaigns
    rather than in the dashboard's service layer."""

    def _run(self, args):
        out = io.StringIO()
        with patch("autoperf.cli.AdbClient", FakeCampaignAdb):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(args)
        return code, out.getvalue()

    def test_start_creates_and_executes_a_repeat_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            code, out = self._run([
                "--db", str(db), "campaign", "start", "--serial", "SERIAL1",
                "--kind", "repeat", "--scenario", "cold_start",
                "--iterations", "3", "--duration", "0.2",
            ])
            result = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(result["count"], 3)
            self.assertEqual(result["executed"], 3)
            self.assertEqual(result["status"], "completed")

            storage = Storage(db)
            runs = storage.list_campaign_runs(result["campaign_id"])
            self.assertTrue(all(run["status"] == "completed" for run in runs))

    def test_soak_start_runs_once_regardless_of_iterations(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            _, out = self._run([
                "--db", str(db), "campaign", "start", "--serial", "SERIAL1",
                "--kind", "soak", "--duration", "0.2", "--iterations", "20",
            ])
            self.assertEqual(json.loads(out)["count"], 1)

    def test_create_only_persists_without_executing(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            _, out = self._run([
                "--db", str(db), "campaign", "start", "--serial", "SERIAL1",
                "--kind", "repeat", "--scenario", "cold_start",
                "--iterations", "2", "--duration", "0.2", "--create-only",
            ])
            campaign_id = json.loads(out)["campaign_id"]
            storage = Storage(db)
            statuses = [r["status"] for r in storage.list_campaign_runs(campaign_id)]
            self.assertEqual(statuses, ["pending", "pending"])

    def test_resume_executes_only_the_remaining_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            _, out = self._run([
                "--db", str(db), "campaign", "start", "--serial", "SERIAL1",
                "--kind", "repeat", "--scenario", "cold_start",
                "--iterations", "3", "--duration", "0.2", "--create-only",
            ])
            campaign_id = json.loads(out)["campaign_id"]
            storage = Storage(db)
            storage.update_run(storage.list_campaign_runs(campaign_id)[0]["id"], "completed")

            code, out = self._run(["--db", str(db), "campaign", "resume", campaign_id])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["executed"], 2)

    def test_invalid_spec_exits_nonzero_without_creating_a_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            code, _ = self._run([
                "--db", str(db), "campaign", "start", "--serial", "SERIAL1",
                "--kind", "repeat", "--duration", "30",
            ])
            self.assertEqual(code, 1)
            self.assertEqual(Storage(db).list_campaigns(), [])

    def test_list_show_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            _, out = self._run([
                "--db", str(db), "campaign", "start", "--serial", "SERIAL1",
                "--kind", "repeat", "--scenario", "cold_start",
                "--iterations", "2", "--duration", "0.2",
            ])
            campaign_id = json.loads(out)["campaign_id"]

            _, listed = self._run(["--db", str(db), "campaign", "list"])
            self.assertEqual(json.loads(listed)[0]["id"], campaign_id)

            _, shown = self._run(["--db", str(db), "campaign", "show", campaign_id])
            detail = json.loads(shown)
            self.assertEqual(detail["status"], "completed")
            self.assertIn("repeat", detail)
            # Child run rows are omitted unless asked for -- a long campaign's
            # full run list would bury the analysis the command exists to show.
            self.assertNotIn("runs", detail)

            _, shown = self._run(["--db", str(db), "campaign", "show", campaign_id, "--runs"])
            self.assertEqual(len(json.loads(shown)["runs"]), 2)

            code, cancelled = self._run(["--db", str(db), "campaign", "cancel", campaign_id])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(cancelled)["campaign_id"], campaign_id)

    def test_unknown_campaign_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            for command in (["campaign", "show", "missing"],
                            ["campaign", "cancel", "missing"],
                            ["campaign", "resume", "missing"]):
                with self.subTest(command=command):
                    code, _ = self._run(["--db", str(db), *command])
                    self.assertEqual(code, 1)


class PreflightCliTests(unittest.TestCase):
    """Check the selectors on one device, then stop -- before a full test."""

    def _run(self, args, adb_class):
        out, err = io.StringIO(), io.StringIO()
        with patch("autoperf.cli.AdbClient", adb_class), patch("time.sleep", lambda _s: None):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_reports_targets_needing_attention_and_exits_nonzero(self):
        code, out, err = self._run(
            ["preflight", "--serial", "SERIAL1", "--scenario", "home_feed_tap_video"],
            StaleSelectorAdb,
        )
        summary = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["needs_attention"])
        # The report has to name the file the fix belongs in.
        self.assertIn("selectors.py", err)

    def test_passes_when_every_selector_matches(self):
        code, out, _ = self._run(
            ["preflight", "--serial", "SERIAL1", "--scenario", "home_feed_tap_video"],
            HealthySelectorAdb,
        )
        summary = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["targets_needing_attention"], 0)

    def test_allow_fallback_downgrades_a_fallback_to_a_note(self):
        code, out, _ = self._run(
            ["preflight", "--serial", "SERIAL1", "--scenario", "home_feed_tap_video",
             "--allow-fallback"],
            StaleSelectorAdb,
        )
        summary = json.loads(out)
        self.assertEqual(code, 0)
        # Still reported -- the flag stops it failing the command, it does not
        # hide the decay.
        self.assertTrue(summary["needs_attention"])

    def test_a_failing_preflight_blocks_a_campaign_from_starting(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            code, _, err = self._run(
                ["--db", str(db), "campaign", "start", "--serial", "SERIAL1",
                 "--kind", "repeat", "--scenario", "home_feed_tap_video",
                 "--iterations", "2", "--duration", "0.2", "--preflight"],
                StaleSelectorAdb,
            )
            self.assertEqual(code, 1)
            self.assertIn("campaign not started", err)
            # Nothing was created: a campaign that aborts partway leaves a
            # half-finished record, which is what preflighting avoids.
            self.assertEqual(Storage(db).list_campaigns(), [])

    def test_the_gate_only_checks_the_scenarios_the_campaign_will_run(self):
        # Blocking a campaign over a selector it never touches would make the
        # gate an obstacle rather than a safeguard.
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            _, _, err = self._run(
                ["--db", str(db), "campaign", "start", "--serial", "SERIAL1",
                 "--kind", "repeat", "--scenario", "home_feed_tap_video",
                 "--iterations", "1", "--duration", "0.2", "--preflight"],
                HealthySelectorAdb,
            )
            self.assertIn("preflight: 1 scenario(s)", err)
            self.assertNotIn("quality_switch_manual", err)

    def test_a_passing_preflight_lets_the_campaign_run(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cli.db"
            code, out, _ = self._run(
                ["--db", str(db), "campaign", "start", "--serial", "SERIAL1",
                 "--kind", "repeat", "--scenario", "home_feed_tap_video",
                 "--iterations", "1", "--duration", "0.2", "--preflight"],
                HealthySelectorAdb,
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["count"], 1)


# The feed rows home_feed_tap_video looks for. Local because "a screen that
# satisfies exactly this scenario" is what these two cases contrast.
FEED_ROWS = (
    '<hierarchy>'
    '<node class="android.view.ViewGroup" clickable="true" bounds="[0,200][1080,800]" content-desc="A"/>'
    '<node class="android.view.ViewGroup" clickable="true" bounds="[0,800][1080,1400]" content-desc="B"/>'
    '</hierarchy>'
)


def StaleSelectorAdb(*_args, **_kwargs):
    """Nothing on screen matches, so every target drops to its coordinate."""
    return DeviceAdb(hierarchy=EMPTY_SCREEN, metrics=True)


def HealthySelectorAdb(*_args, **_kwargs):
    """A screen carrying the feed rows home_feed_tap_video looks for."""
    return DeviceAdb(hierarchy=FEED_ROWS, metrics=True)


def FakeCampaignAdb(*_args, **_kwargs):
    """Answers everything a scenario-driven campaign run asks the device."""
    return DeviceAdb(metrics=True)


if __name__ == "__main__":
    unittest.main()
