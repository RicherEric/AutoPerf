import unittest
from unittest.mock import patch

from autoperf import preflight
from autoperf.adapters import AndroidAdapter
from autoperf.scenarios import youtube
from tests.support import EMPTY_SCREEN, PAUSED, PLAYING, SCREEN, DeviceAdb, NoWaits

# A screen where some of the selector table's entries match and the rest do
# not -- the realistic state of an unverified table. Local to this file
# because that partial-match property is what preflight exists to report on;
# the shared fixtures are deliberately either complete or empty.
PARTIAL_SCREEN = """<hierarchy>
 <node class="android.widget.ImageView" content-desc="搜尋" clickable="true" bounds="[940,80][1040,180]"/>
 <node class="android.widget.Button" content-desc="Shorts" clickable="true" bounds="[600,2250][700,2320]"/>
 <node class="android.view.ViewGroup" clickable="true" bounds="[0,200][1080,800]" content-desc="影片一"/>
 <node class="android.view.ViewGroup" clickable="true" bounds="[0,800][1080,1400]" content-desc="影片二"/>
 <node class="android.view.ViewGroup" clickable="true" bounds="[0,1400][1080,2000]" content-desc="影片三"/>
</hierarchy>"""


def FakeDevice(hierarchy=PARTIAL_SCREEN, *, dump_fails=False, playing=True):
    """This file's default device: the partially-matching screen, playing."""
    return DeviceAdb(hierarchy=hierarchy, dump_fails=dump_fails,
                     playback_state=PLAYING if playing else PAUSED)


def _no_sleep(_seconds):
    """Preflight paces itself to the scenario's own timeline; tests must not."""


class CoveringScenariosTests(unittest.TestCase):
    def test_covers_every_target_the_scenarios_use(self):
        every_target = {
            step.kwargs["target"].name
            for name in youtube.list_scenarios()
            for step in youtube.build(name, SCREEN)
            if step.action == "tap_element"
        }
        covered = {
            step.kwargs["target"].name
            for name in preflight.covering_scenarios()
            for step in youtube.build(name, SCREEN)
            if step.action == "tap_element"
        }
        self.assertEqual(covered, every_target)

    def test_uses_fewer_scenarios_than_running_all_of_them(self):
        # Most presets share _enter_video_steps, so running everything would
        # re-check the same handful of targets many times over.
        self.assertLess(len(preflight.covering_scenarios()), len(youtube.list_scenarios()))

    def test_selection_is_deterministic(self):
        self.assertEqual(preflight.covering_scenarios(), preflight.covering_scenarios())

    def test_can_be_scoped_to_specific_scenarios(self):
        self.assertEqual(preflight.covering_scenarios(["like_video"]), ["like_video"])

    def test_scenarios_with_no_targets_are_skipped(self):
        self.assertEqual(preflight.covering_scenarios(["cold_start", "play_golden"]), [])


class CheckScenarioTests(NoWaits, unittest.TestCase):
    def test_reports_targets_found_by_selector(self):
        device = FakeDevice()
        report = preflight.check_scenario(
            device, AndroidAdapter(), "S1", "home_feed_tap_video", screen=SCREEN, sleep=_no_sleep)

        video = next(c for c in report.targets if c.target == "home_feed_video")
        self.assertEqual(video.status, preflight.MATCHED)
        self.assertEqual(video.strategy, "structural")

    def test_reports_a_target_that_fell_through_to_its_coordinate(self):
        device = FakeDevice(hierarchy=EMPTY_SCREEN)
        report = preflight.check_scenario(
            device, AndroidAdapter(), "S1", "home_feed_tap_video", screen=SCREEN, sleep=_no_sleep)

        video = next(c for c in report.targets if c.target == "home_feed_video")
        self.assertEqual(video.status, preflight.FALLBACK)
        self.assertFalse(report.ok)

    def test_still_taps_when_a_selector_missed(self):
        # The scenario has to keep advancing, or every target on a later
        # screen would be reported broken for reasons unrelated to itself.
        device = FakeDevice(hierarchy=EMPTY_SCREEN)
        preflight.check_scenario(
            device, AndroidAdapter(), "S1", "home_feed_tap_video", screen=SCREEN, sleep=_no_sleep)
        self.assertTrue(device.taps)

    def test_a_failed_ui_dump_is_reported_as_a_fallback_with_its_cause(self):
        # Routine during playback: uiautomator waits for an idle UI and a
        # playing video never is.
        device = FakeDevice(dump_fails=True)
        report = preflight.check_scenario(
            device, AndroidAdapter(), "S1", "home_feed_tap_video", screen=SCREEN, sleep=_no_sleep)

        video = next(c for c in report.targets if c.target == "home_feed_video")
        self.assertEqual(video.status, preflight.FALLBACK)
        self.assertIn("idle state", video.detail)

    def test_captures_what_was_on_screen_when_a_selector_missed(self):
        # The report has to be actionable: seeing what is actually there is
        # how the right selector gets chosen, instead of guessing again.
        device = FakeDevice()
        report = preflight.check_scenario(
            device, AndroidAdapter(), "S1", "like_video", screen=SCREEN, sleep=_no_sleep)

        degraded = [c for c in report.targets if c.status in preflight.DEGRADED]
        self.assertTrue(degraded)
        self.assertTrue(any(c.observed for c in degraded))

    def test_records_verification_outcomes(self):
        report = preflight.check_scenario(
            FakeDevice(), AndroidAdapter(), "S1", "search_and_play", screen=SCREEN, sleep=_no_sleep)
        self.assertTrue(report.verifications)
        self.assertTrue(all(v.result in ("passed", "failed", "unknown") for v in report.verifications))

    def test_every_asserting_action_a_scenario_uses_is_graded(self):
        """An assertion preflight does not grade is an assertion it hides.

        Adding a verify_* action to the adapter and wiring it into a scenario
        without listing it here would make preflight *perform* the check and
        then throw its result away -- it would fall through to the
        navigation branch, which swallows exceptions on purpose.
        """
        used = {
            step.action
            for name in youtube.list_scenarios()
            for step in youtube.build(name, SCREEN)
            if step.action.startswith("verify_")
        }
        self.assertTrue(used <= set(preflight.VERIFICATION_ACTIONS),
                        f"ungraded assertions: {sorted(used - set(preflight.VERIFICATION_ACTIONS))}")

    def test_a_state_assertion_is_graded_by_its_own_outcome(self):
        # Wired through build() rather than called directly, so the grading
        # path is what is under test -- the same route a real scenario takes.
        from autoperf.adapters import ScenarioStep
        from autoperf.scenarios import selectors

        steps = [
            ScenarioStep(0.0, "launch_app", {"package": youtube.PACKAGE}),
            ScenarioStep(0.0, "verify_element_state",
                         {"target": selectors.LIKE_BUTTON, "state": "selected", "expected": True}),
        ]
        with patch.object(youtube, "build", return_value=steps):
            report = preflight.check_scenario(
                FakeDevice(), AndroidAdapter(), "S1", "like_video", screen=SCREEN, sleep=_no_sleep)

        check = next(v for v in report.verifications if v.action == "verify_element_state")
        # PARTIAL_SCREEN has no like button, so the honest answer is "unknown"
        # -- not "failed", which would blame the device for a decayed selector.
        self.assertEqual(check.result, "unknown")

    def test_a_platform_with_no_introspectable_ui_is_not_reported_as_decayed(self):
        """The 19 false findings this removes.

        A Chromecast publishes no hierarchy for the YouTube TV build, so every
        selector missed and every target was reported as having fallen through
        to its coordinate -- a report where each entry was false and none named
        the actual reason. It is not decay and there is nothing to fix.
        """
        from autoperf.profiles import AndroidTvProfile

        report = preflight.check_scenario(
            FakeDevice(hierarchy=EMPTY_SCREEN), AndroidAdapter(), "S1", "home_feed_tap_video",
            screen=SCREEN, sleep=_no_sleep, profile=AndroidTvProfile())

        self.assertTrue(report.targets)
        for check in report.targets:
            with self.subTest(target=check.target):
                self.assertEqual(check.status, preflight.NOT_APPLICABLE)
                self.assertIn("accessibility tree", check.detail)
        # Not a finding, so it does not condemn the scenario.
        self.assertTrue(report.ok)

    def test_the_actions_still_happen_so_later_verifications_mean_something(self):
        # Verification is what works on that platform; it needs the flow to
        # have advanced.
        from autoperf.profiles import AndroidTvProfile

        device = FakeDevice(hierarchy=EMPTY_SCREEN)
        preflight.check_scenario(device, AndroidAdapter(), "S1", "home_feed_tap_video",
                                 screen=SCREEN, sleep=_no_sleep, profile=AndroidTvProfile())
        self.assertTrue(device.taps)

    def test_a_phone_profile_still_grades_selectors(self):
        from autoperf.profiles import PhoneProfile

        report = preflight.check_scenario(
            FakeDevice(hierarchy=EMPTY_SCREEN), AndroidAdapter(), "S1", "home_feed_tap_video",
            screen=SCREEN, sleep=_no_sleep, profile=PhoneProfile())
        self.assertTrue(any(c.status == preflight.FALLBACK for c in report.targets))
        self.assertFalse(report.ok)

    def test_a_failed_verification_makes_the_scenario_not_ok(self):
        report = preflight.check_scenario(
            FakeDevice(playing=False), AndroidAdapter(), "S1", "search_and_play",
            screen=SCREEN, sleep=_no_sleep)
        self.assertTrue(any(v.result == "failed" for v in report.verifications))
        self.assertFalse(report.ok)


class SummariseTests(unittest.TestCase):
    def _report(self, scenario, checks):
        return preflight.ScenarioReport(scenario, checks, [])

    def test_aggregates_a_target_across_the_scenarios_that_use_it(self):
        # The fix is per target, in one table -- so the report is too.
        checks = [
            preflight.TargetCheck("a", 1.0, "search_icon", preflight.FALLBACK, detail="missed"),
            preflight.TargetCheck("b", 1.0, "search_icon", preflight.FALLBACK, detail="missed"),
        ]
        summary = preflight.summarise([self._report("a", checks[:1]), self._report("b", checks[1:])])

        self.assertEqual(summary["targets_needing_attention"], 1)
        entry = summary["needs_attention"][0]
        self.assertEqual(entry["target"], "search_icon")
        self.assertEqual(entry["scenarios"], ["a", "b"])
        self.assertEqual(entry["checked"], 2)

    def test_a_target_matched_everywhere_is_healthy(self):
        checks = [preflight.TargetCheck("a", 1.0, "search_icon", preflight.MATCHED, strategy="content_desc")]
        summary = preflight.summarise([self._report("a", checks)])

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["targets_ok"], 1)
        self.assertEqual(summary["healthy"][0]["strategies"], ["content_desc"])

    def test_a_target_that_only_sometimes_matches_still_needs_attention(self):
        checks = [
            preflight.TargetCheck("a", 1.0, "like_button", preflight.MATCHED, strategy="content_desc"),
            preflight.TargetCheck("b", 1.0, "like_button", preflight.FALLBACK, detail="missed"),
        ]
        summary = preflight.summarise([self._report("a", checks[:1]), self._report("b", checks[1:])])
        self.assertEqual(summary["targets_needing_attention"], 1)
        self.assertEqual(summary["needs_attention"][0]["matched"], 1)

    def test_includes_the_current_selector_table_entry(self):
        checks = [preflight.TargetCheck("a", 1.0, "search_icon", preflight.FALLBACK, detail="missed")]
        entry = preflight.summarise([self._report("a", checks)])["needs_attention"][0]
        # Self-contained: what the table says now, next to what was on screen.
        self.assertTrue(entry["current_selectors"])
        self.assertTrue(any("content_desc" in s for s in entry["current_selectors"]))

    def test_not_applicable_targets_are_neither_findings_nor_healthy(self):
        checks = [preflight.TargetCheck("a", 1.0, "search_icon", preflight.NOT_APPLICABLE,
                                        detail="no accessibility tree here")]
        summary = preflight.summarise([self._report("a", checks)])

        self.assertEqual(summary["targets_needing_attention"], 0)
        self.assertEqual(summary["targets_ok"], 0)
        self.assertEqual(summary["targets_not_applicable"], 1)
        # Claiming it healthy would say selectors work on a platform with no UI
        # to look in; claiming it broken is the false finding this replaces.
        self.assertTrue(summary["ok"])

    def test_a_failed_verification_fails_the_whole_summary(self):
        report = preflight.ScenarioReport(
            "a", [], [preflight.VerificationCheck("a", 1.0, "verify_playing", "failed", "no playback")])
        summary = preflight.summarise([report])
        self.assertFalse(summary["ok"])
        self.assertEqual(len(summary["failed_verifications"]), 1)

    def test_an_unknown_verification_does_not_fail_the_summary(self):
        report = preflight.ScenarioReport(
            "a", [], [preflight.VerificationCheck("a", 1.0, "verify_playing", "unknown", "no state")])
        self.assertTrue(preflight.summarise([report])["ok"])


class RunPreflightTests(NoWaits, unittest.TestCase):
    def test_returns_a_summary_and_writes_nothing(self):
        # A preflight is a question about the device and the selector table,
        # not a measurement -- it must leave no run, no metrics, no history.
        summary = preflight.run_preflight(
            FakeDevice(), AndroidAdapter(), "S1",
            scenarios=["home_feed_tap_video"], sleep=_no_sleep)

        self.assertEqual(summary["serial"], "S1")
        self.assertEqual(summary["screen"], [1080, 2340])
        self.assertEqual(summary["scenarios_checked"], ["home_feed_tap_video"])
        self.assertIn("needs_attention", summary)

    def test_reports_progress_through_callbacks(self):
        seen_scenarios, seen_checks = [], []
        preflight.run_preflight(
            FakeDevice(), AndroidAdapter(), "S1", scenarios=["home_feed_tap_video"],
            sleep=_no_sleep, on_scenario=seen_scenarios.append, on_check=seen_checks.append)

        self.assertEqual(seen_scenarios, ["home_feed_tap_video"])
        self.assertTrue(seen_checks)


if __name__ == "__main__":
    unittest.main()
