from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MetricStats:
    name: str
    count: int
    mean: float
    stdev: float
    minimum: float
    maximum: float


@dataclass(frozen=True, slots=True)
class MetricTrend:
    """How a metric moved *across* a run, as opposed to what it averaged.

    `MetricStats.mean` is close to blind to the failure mode a soak test
    exists to catch. A process leaking memory steadily from 2.0 GB to 2.6 GB
    over eight hours has a mean of 2.3 GB -- perhaps 4% above a healthy run's
    2.2 GB, comfortably under any sane regression threshold, while the device
    is in fact 30% worse off by the end. Slope and start-vs-end drift make
    that visible; mean never will.
    """
    name: str
    slope_per_hour: float
    start_mean: float
    end_mean: float
    drift_pct: float | None
    span_hours: float
    points: int


@dataclass(frozen=True, slots=True)
class MetricComparison:
    name: str
    baseline_mean: float
    candidate_mean: float
    delta_pct: float | None
    regressed: bool


def compute_stats(samples: list[dict]) -> dict[str, MetricStats]:
    grouped: dict[str, list[float]] = {}
    for sample in samples:
        grouped.setdefault(sample["name"], []).append(sample["value"])
    stats: dict[str, MetricStats] = {}
    for name, values in grouped.items():
        stats[name] = MetricStats(
            name=name,
            count=len(values),
            mean=statistics.fmean(values),
            stdev=statistics.pstdev(values) if len(values) > 1 else 0.0,
            minimum=min(values),
            maximum=max(values),
        )
    return stats


def stats_from_aggregates(rows: list[dict]) -> dict[str, MetricStats]:
    """Build MetricStats from `Storage.aggregate_samples()` rows.

    Same output shape as `compute_stats`, but the reduction already happened
    in SQL, so this never holds a run's samples in memory and never truncates.
    Prefer it anywhere a run could be long; `compute_stats` remains for
    callers that already have the sample rows in hand for another reason.
    """
    stats: dict[str, MetricStats] = {}
    for row in rows:
        count = int(row["count"])
        # SQLite hands back NULL for AVG over an empty set, and a
        # single-sample metric has zero variance by definition -- matching
        # compute_stats, which reports stdev 0.0 below two values.
        variance = row["variance"] if row["variance"] is not None else 0.0
        stats[row["name"]] = MetricStats(
            name=row["name"],
            count=count,
            mean=row["mean"],
            # max(0.0, ...) guards the tiny negative a float round-off can
            # produce when every sample is identical.
            stdev=math.sqrt(max(0.0, variance)) if count > 1 else 0.0,
            minimum=row["minimum"],
            maximum=row["maximum"],
        )
    return stats


def compute_trend(buckets: list[dict], edge_fraction: float = 0.25) -> dict[str, MetricTrend]:
    """Per-metric least-squares slope and start-vs-end drift.

    Takes `Storage.downsample_samples()` output rather than raw samples: the
    buckets are already evenly spaced and averaged, which both keeps this
    O(buckets) on a multi-hour run and damps the per-sample noise that would
    otherwise dominate a regression line fitted to jittery 1-second CPU
    readings.

    `slope_per_hour` is in the metric's own unit per hour (KiB/hour for
    memory, %/hour for CPU, C/hour for temperature) -- deliberately not
    normalised, since what counts as an alarming rate differs per metric and
    that judgment belongs to the caller, the same way `compare()` leaves the
    direction of `delta_pct` to whoever reads it.

    `drift_pct` compares the mean of the first `edge_fraction` of the run
    against the last. Slope alone can be misleading when a metric steps once
    and then plateaus (a fitted line reports a gentle constant climb that
    never actually happened); the edge comparison catches the real magnitude
    of the move either way.
    """
    grouped: dict[str, list[dict]] = {}
    for row in buckets:
        grouped.setdefault(row["name"], []).append(row)

    trends: dict[str, MetricTrend] = {}
    for name, rows in grouped.items():
        rows = sorted(rows, key=lambda r: r["bucket"])
        values = [float(r["mean"]) for r in rows]
        times = [_parse_hours(r["timestamp"]) for r in rows]
        origin = times[0]
        hours = [t - origin for t in times]
        span = hours[-1] - hours[0]

        edge = max(1, int(len(rows) * edge_fraction))
        start_mean = statistics.fmean(values[:edge])
        end_mean = statistics.fmean(values[-edge:])
        drift_pct = ((end_mean - start_mean) / start_mean * 100) if start_mean else None

        # A run too short to have two distinct bucket timestamps has no
        # measurable rate of change; reporting 0.0 is honest, dividing by a
        # zero span is not.
        if len(rows) < 2 or span <= 0:
            slope = 0.0
        else:
            mean_x = statistics.fmean(hours)
            mean_y = statistics.fmean(values)
            denominator = sum((x - mean_x) ** 2 for x in hours)
            numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(hours, values))
            slope = numerator / denominator if denominator else 0.0

        trends[name] = MetricTrend(
            name=name,
            slope_per_hour=slope,
            start_mean=start_mean,
            end_mean=end_mean,
            drift_pct=drift_pct,
            span_hours=span,
            points=len(rows),
        )
    return trends


def _parse_hours(timestamp: str) -> float:
    """ISO-8601 timestamp -> absolute hours, for use as a regression x-axis."""
    return datetime.fromisoformat(timestamp).timestamp() / 3600.0


def app_version_delta(baseline_run: dict | None, candidate_run: dict | None) -> dict:
    """Whether two runs measured the same build of the app under test.

    Stated explicitly, as its own field, because it changes what a comparison
    *means* rather than merely annotating it. If the app updated between a
    baseline and its candidate, the delta describes the app's change, not the
    device's -- and a background app update is the more likely explanation of
    a sudden shift, not the less likely one. Reporting a 30% regression
    without saying the app version moved underneath it is reporting the wrong
    cause.

    `changed` is None when either side has no recorded version -- runs
    predating version capture, or a package whose version could not be read.
    Unknown is not the same as unchanged, and must not be shown as such.
    """
    def describe(run: dict | None) -> dict | None:
        if not run or not (run.get("app_version_name") or run.get("app_version_code")):
            return None
        return {
            "package": run.get("app_package"),
            "version_name": run.get("app_version_name"),
            "version_code": run.get("app_version_code"),
        }

    baseline_version, candidate_version = describe(baseline_run), describe(candidate_run)
    if baseline_version is None or candidate_version is None:
        changed = None
    else:
        changed = (
            baseline_version["version_code"] != candidate_version["version_code"]
            or baseline_version["version_name"] != candidate_version["version_name"]
        )
    return {"changed": changed, "baseline": baseline_version, "candidate": candidate_version}


def compare(
    baseline: dict[str, MetricStats],
    candidate: dict[str, MetricStats],
    threshold_pct: float = 20.0,
) -> list[MetricComparison]:
    """Compare candidate metric stats against a baseline.

    Doesn't assume whether "higher" or "lower" is worse for a given metric
    name -- future collectors could add metrics where either direction is
    fine or bad, and hardcoding that per-name is a judgment call this layer
    shouldn't make. Instead it just flags metrics whose mean moved beyond
    `threshold_pct` in either direction, leaving direction visible in
    `delta_pct` for the caller (CLI/dashboard) to interpret.
    """
    comparisons = []
    for name, baseline_stats in baseline.items():
        candidate_stats = candidate.get(name)
        if candidate_stats is None:
            continue
        if baseline_stats.mean == 0:
            delta_pct = None
            regressed = candidate_stats.mean != 0
        else:
            delta_pct = (candidate_stats.mean - baseline_stats.mean) / baseline_stats.mean * 100
            regressed = abs(delta_pct) > threshold_pct
        comparisons.append(MetricComparison(
            name=name,
            baseline_mean=baseline_stats.mean,
            candidate_mean=candidate_stats.mean,
            delta_pct=delta_pct,
            regressed=regressed,
        ))
    return comparisons
