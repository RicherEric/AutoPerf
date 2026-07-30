"""Long-running test campaigns.

A campaign is one test programme that spawns many ordinary runs -- either a
`soak` (a single continuous run of several hours, to expose drift and leaks)
or a `repeat` (the same scenario, or a whole tier, run N times, to measure
flakiness and metric spread). Its child runs are plain `test_runs` rows
tagged with a `campaign_id`, so baselines, comparison, live screen,
recordings and deletion all keep working on them with no special-casing.

This module lives in the framework core, not in the dashboard, so a campaign
can be planned, executed and analysed with no Django, Celery or Redis
present -- the same offline-first, framework-first property the rest of
`autoperf` has. The dashboard adds only its own dispatch strategy on top:
it pre-enqueues each child run as a Celery task instead of running the loop
in-process (see `execute_campaign` for why the two differ).
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass, replace

from .adapters import Adapter
from .adb import AdbClientProtocol
from .analyzer import compare, compute_trend, stats_from_aggregates
from .profiles import select_profile
from .models import RunStatus
from .runner import TestRunner
from .scenarios import youtube as youtube_scenarios
from .storage import Storage

KINDS = ("soak", "repeat")

# A ceiling on how many child runs one campaign may create. Not a resource
# limit so much as a typo guard: "repeat the regression tier 10000 times" is
# far more likely to be a slipped digit than an intent.
MAX_RUNS = 500

TERMINAL_RUN_STATUSES = ("completed", "failed", "interrupted")

DEFAULT_REGRESSION_THRESHOLD_PCT = 20.0

# Buckets used when reducing a soak run for trend fitting -- enough shape to
# fit a line against without refitting over ~100k raw rows.
SOAK_TREND_BUCKETS = 200


@dataclass(frozen=True, slots=True)
class CampaignSpec:
    kind: str
    serial: str
    duration: float
    scenario: str | None = None
    tier: str | None = None
    iterations: int = 1

    def validated(self) -> CampaignSpec:
        """Return a normalised copy, raising ValueError on anything invalid."""
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if not self.serial:
            raise ValueError("serial is required")
        if self.duration <= 0:
            raise ValueError("duration must be positive")
        if self.tier and self.tier not in youtube_scenarios.TIERS:
            raise ValueError(f"tier must be one of {youtube_scenarios.TIERS}")

        if self.kind == "repeat":
            if not self.scenario and not self.tier:
                raise ValueError("a repeat campaign needs either a scenario or a tier")
            if self.iterations < 1:
                raise ValueError("iterations must be at least 1")
            spec = self
        else:
            # A soak is one continuous run by definition. Silently honouring
            # an iteration count here would run something other than what
            # the caller asked for.
            spec = replace(self, iterations=1, tier=None)

        planned = len(spec.planned_scenarios())
        if planned > MAX_RUNS:
            raise ValueError(f"campaign would enqueue {planned} runs, above the limit of {MAX_RUNS}")
        return spec

    def planned_scenarios(self) -> list[str | None]:
        """The ordered scenario for each child run.

        Tier campaigns are ordered iteration-major (a full sweep of the tier,
        then the next full sweep) rather than scenario-major. It matters when
        a campaign is cut short: stopping halfway through an iteration-major
        campaign leaves an even number of samples for every scenario, whereas
        scenario-major would leave the first few scenarios fully sampled and
        the rest with none, making any cross-scenario comparison worthless.

        A `None` entry means a plain sampling run with nothing driving the
        device, which is a legitimate thing to soak.
        """
        if self.kind == "soak":
            return [self.scenario]
        if self.tier:
            names = youtube_scenarios.list_scenarios(tier=self.tier)
            return [name for _ in range(self.iterations) for name in names]
        return [self.scenario] * self.iterations


def create_campaign(storage: Storage, spec: CampaignSpec) -> dict:
    """Persist a campaign and all of its child run rows.

    Every run row is created up front, in `pending` state, rather than one at
    a time as execution reaches it. That makes the campaign's full extent
    visible immediately -- progress is `finished / total` from the first
    moment rather than a total that grows as it goes -- and it is what lets
    the dashboard and the CLI share one progress and cancellation model
    despite executing the runs very differently.
    """
    spec = spec.validated()
    campaign_id = uuid.uuid4().hex
    storage.create_campaign(
        campaign_id, spec.kind, spec.serial, spec.duration,
        scenario=spec.scenario, tier=spec.tier, iterations=spec.iterations,
    )
    run_ids = []
    for scenario in spec.planned_scenarios():
        run_id = uuid.uuid4().hex
        storage.create_run(run_id, spec.serial, scenario, campaign_id=campaign_id)
        run_ids.append(run_id)
    return {"campaign_id": campaign_id, "run_ids": run_ids, "count": len(run_ids)}


def execute_campaign(storage: Storage, adb: AdbClientProtocol, campaign_id: str, *,
                     adapter_factory=None, on_run=None) -> dict:
    """Run a campaign's child runs in this process, one after another.

    This is the CLI's execution strategy. The dashboard deliberately does
    *not* use it: there, each child run is pre-enqueued as its own Celery
    task, because a single long-lived task would monopolise the
    Windows-compatible `--pool=solo` worker for the campaign's whole duration
    and, since Celery re-delivers unacknowledged tasks, would silently
    restart from iteration one after a worker restart. In a CLI process
    neither concern applies -- the process *is* the campaign -- so a plain
    in-process loop is both simpler and easier to follow at a terminal.

    Runs already in a terminal state are skipped, which makes re-invoking
    this on an interrupted campaign resume it rather than duplicate work.

    `on_run` is called with each finished run row, letting a caller report
    progress without this module knowing anything about output formatting.
    """
    campaign = storage.get_campaign(campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")

    serial, duration = campaign["device_serial"], campaign["duration"]
    # Probed once for the whole campaign: the device cannot change platform
    # between child runs, and a campaign is exactly where re-probing per
    # iteration would multiply a shell call by a hundred for no new answer.
    profile = select_profile(adb, serial)
    storage.update_campaign(campaign_id, RunStatus.RUNNING)
    cancelled = False
    executed = []

    for run in storage.list_campaign_runs(campaign_id):
        # Re-read per iteration: a cancel can arrive from anywhere (another
        # process, the dashboard) while this loop is running.
        current = storage.get_campaign(campaign_id)
        if current and current.get("cancel_requested"):
            cancelled = True
            break
        if run["status"] in TERMINAL_RUN_STATUSES:
            continue

        scenario_name = run["youtube_scenario"]
        adapter: Adapter | None = None
        scenario = None
        try:
            if scenario_name:
                adapter = adapter_factory(adb, serial) if adapter_factory else profile.adapter()
                scenario = youtube_scenarios.build(scenario_name, adapter.screen_size(adb, serial))
            TestRunner(storage, adb, profile.collectors(), adapter=adapter, scenario=scenario).run(
                serial, duration, run["id"]
            )
        except Exception as exc:
            # One failed run must not abandon the rest of the campaign --
            # a campaign exists precisely to gather many samples, and a
            # single device hiccup is data, not a reason to stop.
            storage.update_run(run["id"], RunStatus.FAILED, error=str(exc))

        finished = storage.get_run(run["id"])
        executed.append(finished)
        # Seed a baseline from the first successful run of each scenario, so
        # a repeat campaign has something to compare its later iterations
        # against. Mirrors what the Celery path does for dashboard runs.
        if (finished and finished["status"] == "completed"
                and storage.get_baseline(serial, scenario_name) is None):
            storage.set_baseline(serial, run["id"])
        if on_run is not None:
            on_run(finished)

    runs = storage.list_campaign_runs(campaign_id)
    status = RunStatus.INTERRUPTED if cancelled else _status_from_runs(runs)
    storage.update_campaign(campaign_id, status)
    return {"campaign_id": campaign_id, "executed": len(executed), "status": str(status)}


def cancel_campaign(storage: Storage, campaign_id: str) -> dict:
    """Cancel a campaign and everything it still has queued or running."""
    if storage.get_campaign(campaign_id) is None:
        raise ValueError("campaign not found")
    storage.request_campaign_cancel(campaign_id)
    flagged = storage.cancel_campaign_runs(campaign_id)
    storage.update_campaign(campaign_id, RunStatus.INTERRUPTED)
    return {"campaign_id": campaign_id, "cancelled_runs": flagged, "status": "cancelling"}


def _status_from_runs(runs: list[dict]) -> str:
    if runs and all(run["status"] in TERMINAL_RUN_STATUSES for run in runs):
        return RunStatus.COMPLETED
    return RunStatus.RUNNING


def derived_status(campaign: dict, runs: list[dict]) -> str:
    """A campaign's status, computed from its child runs.

    Under the dashboard's dispatch strategy nothing orchestrates a campaign
    while it executes, so no process is in a position to write "completed" at
    the right moment. Deriving it on read is what keeps the status honest
    across a worker restart, a cancelled campaign, and runs a busy device is
    still retrying -- and it means the CLI and dashboard agree on status
    without sharing an executor.
    """
    if campaign.get("cancel_requested"):
        return "interrupted"
    if not runs:
        return campaign["status"]
    if all(run["status"] in TERMINAL_RUN_STATUSES for run in runs):
        return "completed"
    return "running"


def _run_metric_means(storage: Storage, run_id: str) -> dict[str, float]:
    return {name: stat.mean for name, stat in
            stats_from_aggregates(storage.aggregate_samples(run_id)).items()}


def repeat_analysis(storage: Storage, runs: list[dict], threshold_pct: float) -> dict:
    """Per-scenario stability across repeated identical runs.

    Two failure modes are counted separately rather than folded into one pass
    rate, because they call for different responses: a run that never reached
    `completed` is a broken test or a device problem, while a completed run
    whose metrics regressed past the threshold is a performance finding.

    A scenario is `flaky` when the same scenario, run repeatedly against the
    same device for the same duration, produced both passes and failures.
    With every input held constant a mixed outcome is the definition of
    flakiness -- and it is exactly what no single run can reveal.
    """
    by_scenario: dict[str, list[dict]] = {}
    for run in runs:
        by_scenario.setdefault(run["youtube_scenario"] or "", []).append(run)

    baseline_cache: dict[tuple[str, str], dict | None] = {}
    scenarios = []
    for scenario, scenario_runs in sorted(by_scenario.items()):
        errored = [r for r in scenario_runs if r["status"] in ("failed", "interrupted")]
        pending = [r for r in scenario_runs if r["status"] not in TERMINAL_RUN_STATUSES]
        finished_ok = [r for r in scenario_runs if r["status"] == "completed"]

        # An iteration whose scenario could not be carried out measured a
        # screen it never reached. Including it would corrupt both outputs
        # this analysis exists for: its metrics would widen the spread for a
        # reason unrelated to device variance, and it would register as a
        # differing outcome, reporting a scenario as flaky when the truth is
        # that the test itself is broken.
        unverified = [r for r in finished_ok if not storage.run_quality(r["id"])["verified"]]
        unverified_ids = {r["id"] for r in unverified}
        completed = [r for r in finished_ok if r["id"] not in unverified_ids]

        regressed, metric_means = [], {}
        for run in completed:
            for name, value in _run_metric_means(storage, run["id"]).items():
                metric_means.setdefault(name, []).append(value)

            cache_key = (run["device_serial"], scenario)
            if cache_key not in baseline_cache:
                baseline_row = storage.get_baseline(run["device_serial"], scenario)
                baseline_cache[cache_key] = (
                    stats_from_aggregates(storage.aggregate_samples(baseline_row["run_id"]))
                    if baseline_row else None
                )
            baseline_stats = baseline_cache[cache_key]
            if baseline_stats is not None:
                results = compare(
                    baseline_stats,
                    stats_from_aggregates(storage.aggregate_samples(run["id"])),
                    threshold_pct=threshold_pct,
                )
                if any(r.regressed for r in results):
                    regressed.append(run["id"])

        # Spread of each metric's per-run mean across iterations. A metric
        # that swings widely between identical runs makes any single-run
        # baseline comparison unreliable, so it is reported next to the pass
        # counts rather than left to be inferred.
        stability = []
        for name, values in sorted(metric_means.items()):
            mean_of_means = statistics.fmean(values)
            spread = statistics.pstdev(values) if len(values) > 1 else 0.0
            stability.append({
                "name": name,
                "runs": len(values),
                "mean": mean_of_means,
                "stdev": spread,
                # Coefficient of variation: spread relative to the metric's
                # own level, so cpu % and memory KiB stay comparable.
                "cv_pct": (spread / mean_of_means * 100) if mean_of_means else None,
                "minimum": min(values),
                "maximum": max(values),
            })

        finished = len(completed) + len(errored)
        passed = len(completed) - len(regressed)
        scenarios.append({
            "scenario": scenario,
            "total": len(scenario_runs),
            "pending": len(pending),
            "completed": len(completed),
            "unverified": len(unverified),
            "unverified_run_ids": [r["id"] for r in unverified],
            "errored": len(errored),
            "regressed": len(regressed),
            "regressed_run_ids": regressed,
            "pass_rate": (passed / finished * 100) if finished else None,
            "flaky": finished > 1 and 0 < passed < finished,
            "metric_stability": stability,
        })
    return {"scenarios": scenarios}


def soak_analysis(storage: Storage, runs: list[dict], buckets: int = SOAK_TREND_BUCKETS) -> dict:
    """Drift/leak verdict for a soak campaign's long run.

    Reads through `downsample_samples` rather than raw rows: a soak run is
    precisely the case where loading every sample is unaffordable, and
    `compute_trend` wants evenly spaced points regardless.
    """
    candidates = [r for r in runs if r["status"] in ("completed", "running")] or runs
    if not candidates:
        return {"run_id": None, "status": None, "trends": []}
    run = candidates[0]
    trends = compute_trend(storage.downsample_samples(run["id"], buckets=buckets))
    return {
        "run_id": run["id"],
        "status": run["status"],
        "trends": [
            {
                "name": trend.name,
                "slope_per_hour": trend.slope_per_hour,
                "start_mean": trend.start_mean,
                "end_mean": trend.end_mean,
                "drift_pct": trend.drift_pct,
                "span_hours": trend.span_hours,
                "points": trend.points,
            }
            for trend in sorted(trends.values(), key=lambda t: t.name)
        ],
    }


def campaign_detail(storage: Storage, campaign_id: str,
                    threshold_pct: float = DEFAULT_REGRESSION_THRESHOLD_PCT) -> dict:
    campaign = storage.get_campaign(campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    runs = storage.list_campaign_runs(campaign_id)
    finished = sum(1 for run in runs if run["status"] in TERMINAL_RUN_STATUSES)
    detail = {
        **campaign,
        "status": derived_status(campaign, runs),
        "stored_status": campaign["status"],
        "run_count": len(runs),
        "finished_count": finished,
        "progress_pct": (finished / len(runs) * 100) if runs else 0.0,
        "threshold_pct": threshold_pct,
        "runs": runs,
    }
    if campaign["kind"] == "soak":
        detail["soak"] = soak_analysis(storage, runs)
    else:
        detail["repeat"] = repeat_analysis(storage, runs, threshold_pct)
    return detail


def campaign_summaries(storage: Storage, limit: int = 50,
                       device_serial: str | None = None) -> list[dict]:
    """Campaigns newest-first with derived status and progress.

    Uses the counts `Storage.list_campaigns` already computes in its grouped
    join instead of re-reading every run row, so this stays one round trip
    per call -- it is polled while campaigns are running.
    """
    campaigns = storage.list_campaigns(limit=limit, device_serial=device_serial)
    for campaign in campaigns:
        total = campaign["run_count"] or 0
        finished = (campaign["completed_count"] or 0) + (campaign["failed_count"] or 0)
        campaign["stored_status"] = campaign["status"]
        if campaign.get("cancel_requested"):
            campaign["status"] = "interrupted"
        elif total and finished == total:
            campaign["status"] = "completed"
        elif total:
            campaign["status"] = "running"
        campaign["finished_count"] = finished
        campaign["progress_pct"] = (finished / total * 100) if total else 0.0
    return campaigns
