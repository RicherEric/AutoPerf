from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .adb import AdbClientProtocol

_PACKAGE_RE = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z][\w]*)+$")
_ACTIVITY_RE = re.compile(r"^\.?[A-Za-z][\w.]*$")
_KEYCODE_RE = re.compile(r"^KEYCODE_[A-Z0-9_]+$")

HOME = "KEYCODE_HOME"
BACK = "KEYCODE_BACK"
APP_SWITCH = "KEYCODE_APP_SWITCH"


def _require_package(package: str) -> str:
    if not _PACKAGE_RE.fullmatch(package):
        raise ValueError(f"Invalid Android package name: {package!r}")
    return package


def _require_activity(activity: str) -> str:
    if not _ACTIVITY_RE.fullmatch(activity):
        raise ValueError(f"Invalid Android activity name: {activity!r}")
    return activity


def _require_keycode(keycode: str) -> str:
    if not _KEYCODE_RE.fullmatch(keycode):
        raise ValueError(f"Invalid Android keycode: {keycode!r}")
    return keycode


@dataclass(slots=True)
class Adapter(ABC):
    name: str = "adapter"

    @abstractmethod
    def launch_app(self, adb: AdbClientProtocol, serial: str, package: str, activity: str | None = None) -> None: ...

    @abstractmethod
    def stop_app(self, adb: AdbClientProtocol, serial: str, package: str) -> None: ...

    @abstractmethod
    def tap(self, adb: AdbClientProtocol, serial: str, x: int, y: int) -> None: ...

    @abstractmethod
    def swipe(self, adb: AdbClientProtocol, serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None: ...

    @abstractmethod
    def key_event(self, adb: AdbClientProtocol, serial: str, keycode: str) -> None: ...


class AndroidAdapter(Adapter):
    """Generic AOSP adapter: drives the device with plain `adb shell input`/`am`/`monkey`
    commands only, no OEM-private APIs.

    OEM-specific hook point: subclass AndroidAdapter and override individual
    methods once a *real, observed* behavioral difference exists (e.g. a One UI
    dialog that must be dismissed before `am start` works, or a TV remote with no
    touchscreen that needs tap/swipe mapped to KEYCODE_DPAD_* instead). Don't
    pre-create empty subclasses before there's OEM logic to put in them.
    """

    def __init__(self):
        super().__init__("android")

    def launch_app(self, adb, serial, package, activity=None):
        package = _require_package(package)
        if activity:
            adb.shell(serial, f"am start -n {package}/{_require_activity(activity)}")
        else:
            adb.shell(serial, f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def stop_app(self, adb, serial, package):
        adb.shell(serial, f"am force-stop {_require_package(package)}")

    def tap(self, adb, serial, x, y):
        adb.shell(serial, f"input tap {int(x)} {int(y)}")

    def swipe(self, adb, serial, x1, y1, x2, y2, duration_ms=300):
        adb.shell(serial, f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def key_event(self, adb, serial, keycode):
        adb.shell(serial, f"input keyevent {_require_keycode(keycode)}")


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    at: float
    action: str
    kwargs: dict[str, Any] = field(default_factory=dict)
