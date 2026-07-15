from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetricStats:
    name: str
    count: int
    mean: float
    stdev: float
    minimum: float
    maximum: float


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
