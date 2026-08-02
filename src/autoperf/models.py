from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class RunOrigin(StrEnum):
    """What asked for a run.

    Kept apart from `campaign_id` because they answer different questions.
    `campaign_id` says which batch a run belongs to; this says who started it,
    and a reader comparing two trends needs that: a series built from runs
    someone triggered by hand while watching is not the same evidence as one
    a scheduler produced unattended, even when every number matches.

    `UNKNOWN` is for rows written before the column existed. It is a real
    value rather than NULL so that "we never recorded this" is visible in a
    facet count instead of vanishing from it.
    """

    MANUAL = "manual"          # a person at a terminal: `autoperf run`
    DASHBOARD = "dashboard"    # queued from the web UI
    CAMPAIGN = "campaign"      # one iteration of a repeat/soak campaign
    SUITE = "suite"            # part of a scenario suite run
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Device:
    serial: str
    state: str
    model: str = "unknown"
    product: str = "unknown"
    transport_id: str | None = None


@dataclass(frozen=True, slots=True)
class MetricSample:
    run_id: str
    collector: str
    name: str
    value: float
    unit: str
    timestamp: str = field(default_factory=utc_now)
    labels: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TestEvent:
    run_id: str
    kind: str
    message: str
    timestamp: str = field(default_factory=utc_now)
    details: dict[str, Any] = field(default_factory=dict)
