from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .adb import AdbClientProtocol
from .models import MetricSample


@dataclass(slots=True)
class Collector(ABC):
    interval: float = 5.0
    name: str = "collector"

    @abstractmethod
    def collect(self, adb: AdbClientProtocol, serial: str, run_id: str) -> list[MetricSample]: ...


class CpuCollector(Collector):
    def __init__(self, interval: float = 5.0):
        super().__init__(interval, "cpu")

    def collect(self, adb: AdbClientProtocol, serial: str, run_id: str) -> list[MetricSample]:
        output = adb.shell(serial, "dumpsys cpuinfo")
        match = re.search(r"([\d.]+)%\s+TOTAL", output, re.IGNORECASE)
        if not match:
            raise ValueError("Unable to parse total CPU usage")
        return [MetricSample(run_id, self.name, "cpu.total", float(match.group(1)), "%")]


class MemoryCollector(Collector):
    def __init__(self, interval: float = 5.0):
        super().__init__(interval, "memory")

    def collect(self, adb: AdbClientProtocol, serial: str, run_id: str) -> list[MetricSample]:
        output = adb.shell(serial, "cat /proc/meminfo")
        values = {key: float(value) for key, value in re.findall(r"^(MemTotal|MemAvailable):\s+(\d+)", output, re.MULTILINE)}
        if len(values) != 2:
            raise ValueError("Unable to parse /proc/meminfo")
        used = values["MemTotal"] - values["MemAvailable"]
        return [MetricSample(run_id, self.name, "memory.used", used, "KiB")]


class BatteryCollector(Collector):
    def __init__(self, interval: float = 10.0):
        # Battery level/temperature change slowly, but a run shorter than one
        # interval only ever gets a single sample -- no trend line at all on
        # the dashboard's chart. 10s (vs cpu/memory's 5s) still cuts the
        # dumpsys call rate in half while giving most runs several points.
        super().__init__(interval, "battery")

    def collect(self, adb: AdbClientProtocol, serial: str, run_id: str) -> list[MetricSample]:
        output = adb.shell(serial, "dumpsys battery")
        fields = dict(re.findall(r"^\s*(level|temperature):\s*(-?\d+)", output, re.MULTILINE))
        samples: list[MetricSample] = []
        if "level" in fields:
            samples.append(MetricSample(run_id, self.name, "battery.level", float(fields["level"]), "%"))
        if "temperature" in fields:
            samples.append(MetricSample(run_id, self.name, "battery.temperature", float(fields["temperature"]) / 10, "C"))
        if not samples:
            raise ValueError("Unable to parse battery state")
        return samples


def default_collectors() -> list[Collector]:
    return [CpuCollector(), MemoryCollector(), BatteryCollector()]
