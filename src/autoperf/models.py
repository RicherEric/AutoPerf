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
