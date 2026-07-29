from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .adb import AdbClientProtocol

_PACKAGE_RE = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z][\w]*)+$")
_ACTIVITY_RE = re.compile(r"^\.?[A-Za-z][\w.]*$")
_KEYCODE_RE = re.compile(r"^KEYCODE_[A-Z0-9_]+$")
_WM_SIZE_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")
_URI_RE = re.compile(r"^https://[A-Za-z0-9./:?=_&%-]+$")

HOME = "KEYCODE_HOME"
BACK = "KEYCODE_BACK"
APP_SWITCH = "KEYCODE_APP_SWITCH"
DPAD_UP = "KEYCODE_DPAD_UP"
DPAD_DOWN = "KEYCODE_DPAD_DOWN"
DPAD_LEFT = "KEYCODE_DPAD_LEFT"
DPAD_RIGHT = "KEYCODE_DPAD_RIGHT"
DPAD_CENTER = "KEYCODE_DPAD_CENTER"


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


def _require_uri(uri: str) -> str:
    if not _URI_RE.fullmatch(uri):
        raise ValueError(f"Invalid or unsafe URI: {uri!r}")
    return uri


@dataclass(slots=True)
class Adapter(ABC):
    name: str = "adapter"

    @abstractmethod
    def launch_app(self, adb: AdbClientProtocol, serial: str, package: str, activity: str | None = None,
                    data: str | None = None) -> None: ...

    @abstractmethod
    def stop_app(self, adb: AdbClientProtocol, serial: str, package: str) -> None: ...

    @abstractmethod
    def tap(self, adb: AdbClientProtocol, serial: str, x: int, y: int) -> None: ...

    @abstractmethod
    def swipe(self, adb: AdbClientProtocol, serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None: ...

    @abstractmethod
    def key_event(self, adb: AdbClientProtocol, serial: str, keycode: str) -> None: ...

    @abstractmethod
    def screen_size(self, adb: AdbClientProtocol, serial: str) -> tuple[int, int]: ...


class VerificationError(AssertionError):
    """A scenario step could not be satisfied on the device.

    Raised rather than returning quietly because `adb shell input tap`
    reports success for any on-screen coordinate, occupied or not. Without
    an exception the runner records "adapter_action completed" and the run
    ends green having measured an untouched screen. TestRunner turns this
    into a `verification_failed` event, which in turn marks the run
    unverified -- so a decayed selector shows up as missing data rather than
    as a plausible-looking result.
    """


class ElementNotFound(VerificationError):
    pass


# Mixed into both adapters below. Kept separate from Adapter itself so the
# abstract interface stays the minimal set a plug-in must implement --
# these are all built from launch/tap/swipe plus a UI dump.
class ElementActionsMixin:
    # A scenario step fires at its scripted time, which is a guess about how
    # long the app needs. Observed on a Galaxy A55: `subscriptions_feed_browse`
    # taps the Subscriptions tab at t=3.0s, and at that moment YouTube has
    # rendered its containers but not yet its bottom navigation -- so a
    # single-shot lookup found nothing and reported the selector as decayed
    # when the real problem was arriving early. Retrying briefly turns that
    # common case back into a match; the budget stays well inside
    # TestRunner.adapter_action_timeout.
    RESOLVE_ATTEMPTS = 3
    RESOLVE_RETRY_DELAY = 1.0

    def _resolve(self, adb: AdbClientProtocol, serial: str, target, screen=None):
        from . import uiauto

        screen = screen or self.screen_size(adb, serial)
        resolution = None
        for attempt in range(self.RESOLVE_ATTEMPTS):
            try:
                nodes = uiauto.parse_hierarchy(uiauto.dump_hierarchy(adb, serial))
            except Exception:
                # A failed dump must not be fatal on its own: the coordinate
                # fallback is exactly as good as the behaviour that preceded
                # this module, so degrade to it rather than failing the step.
                nodes = []
            resolution = uiauto.resolve(target, nodes, screen)
            # Only a *selector* match ends the retry loop. Stopping at the
            # coordinate fallback would defeat the point, since the fallback
            # is available on the first attempt and every attempt after it.
            if resolution is not None and resolution.strategy != "coordinates":
                break
            if attempt < self.RESOLVE_ATTEMPTS - 1:
                time.sleep(self.RESOLVE_RETRY_DELAY)
        return resolution, screen

    def tap_element(self, adb: AdbClientProtocol, serial: str, target, screen=None) -> dict:
        """Tap an element located by selector chain, coordinates last.

        Returns which strategy matched so the runner can record it: a step
        that quietly fell through to `coordinates` still worked, but it is
        the early warning that a selector has gone stale, and it is invisible
        unless reported.
        """
        resolution, _ = self._resolve(adb, serial, target, screen)
        if resolution is None:
            raise ElementNotFound(
                f"no element matched {target.name or 'target'} and no coordinate fallback was given"
            )
        x, y = resolution.point
        self.tap(adb, serial, x, y)
        return {"target": target.name, "strategy": resolution.strategy, "x": x, "y": y}

    def verify_foreground(self, adb: AdbClientProtocol, serial: str, package: str) -> dict:
        """Assert `package` is actually the app in front."""
        from . import uiauto

        focus = uiauto.current_focus(adb, serial)
        if focus is None:
            # Unreadable focus is not evidence of failure; saying so beats
            # failing a run because dumpsys was momentarily unavailable.
            return {"package": package, "verified": None}
        if focus[0] != package:
            raise VerificationError(f"expected {package} in foreground, found {focus[0]}")
        return {"package": package, "verified": True, "activity": focus[1]}

    def verify_playing(self, adb: AdbClientProtocol, serial: str, package: str | None = None) -> dict:
        """Assert media is actually playing.

        The strongest check available, and the only one that separates
        "search_and_play worked" from "search_and_play tapped four times into
        empty space and left the home feed on screen" -- a foreground check
        passes in both cases.
        """
        from . import uiauto

        playing = uiauto.is_playing(adb, serial, package)
        if playing is None:
            return {"verified": None}
        if not playing:
            raise VerificationError("expected active media playback, found none")
        return {"verified": True}


class AndroidAdapter(ElementActionsMixin, Adapter):
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

    def launch_app(self, adb, serial, package, activity=None, data=None):
        package = _require_package(package)
        if data:
            adb.shell(serial, f'am start -a android.intent.action.VIEW -d "{_require_uri(data)}" {package}')
        elif activity:
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

    def screen_size(self, adb, serial):
        output = adb.shell(serial, "wm size")
        match = _WM_SIZE_RE.search(output)
        if not match:
            raise ValueError("Unable to parse screen size")
        return int(match.group(1)), int(match.group(2))


class AndroidTvAdapter(AndroidAdapter):
    """Maps phone-oriented scenarios onto Android TV packages and DPAD input."""

    _PACKAGE_MAP = {
        "com.android.settings": ("com.android.tv.settings", ".MainSettings"),
        "com.google.android.youtube": ("com.google.android.youtube.tv", None),
    }

    def __init__(self):
        Adapter.__init__(self, "android-tv")

    def launch_app(self, adb, serial, package, activity=None, data=None):
        package, mapped_activity = self._PACKAGE_MAP.get(package, (package, None))
        super().launch_app(adb, serial, package, mapped_activity or activity, data)

    def stop_app(self, adb, serial, package):
        super().stop_app(adb, serial, self.mapped_package(package))

    def mapped_package(self, package: str) -> str:
        """The package this adapter actually drives for `package`.

        Scenarios are written against the phone package names; the TV variants
        are substituted here. Verification has to apply the same substitution
        or it compares a scenario's `com.google.android.youtube` against the
        `com.google.android.youtube.tv` that was really launched and fails
        every single time -- observed on a Chromecast, where it marked
        otherwise healthy runs unverified.
        """
        mapped, _ = self._PACKAGE_MAP.get(package, (package, None))
        return mapped

    def verify_foreground(self, adb, serial, package):
        return super().verify_foreground(adb, serial, self.mapped_package(package))

    def verify_playing(self, adb, serial, package=None):
        return super().verify_playing(
            adb, serial, self.mapped_package(package) if package else None)

    def tap(self, adb, serial, x, y):
        self.key_event(adb, serial, DPAD_CENTER)

    def swipe(self, adb, serial, x1, y1, x2, y2, duration_ms=300):
        dx, dy = int(x2) - int(x1), int(y2) - int(y1)
        if abs(dy) >= abs(dx):
            keycode = DPAD_DOWN if dy < 0 else DPAD_UP
        else:
            keycode = DPAD_RIGHT if dx < 0 else DPAD_LEFT
        self.key_event(adb, serial, keycode)

    # How many DPAD presses focus may take to reach a target before the step
    # is called a failure. Bounded because a target that focus cannot reach
    # (off-screen, unfocusable) would otherwise loop until the run ended.
    MAX_FOCUS_STEPS = 12

    def tap_element(self, adb, serial, target, screen=None):
        """Move focus onto the element, then press select.

        Inheriting the phone implementation would locate the element
        correctly and then press DPAD_CENTER regardless -- since `tap` on a
        TV discards its coordinates -- activating whatever happened to be
        focused. That is the same silent-wrong-action problem this whole
        layer exists to remove, just one level up.

        So focus is actually walked toward the target: compare the focused
        node's centre with the target's, press the DPAD key that closes the
        larger gap, re-read the hierarchy, repeat. The coordinate fallback is
        deliberately *not* honoured here -- a pixel is not something a remote
        control can address, and pretending otherwise is what produced
        meaningless TV runs before.
        """
        from . import uiauto

        screen = screen or self.screen_size(adb, serial)
        for _ in range(self.MAX_FOCUS_STEPS):
            nodes = uiauto.parse_hierarchy(uiauto.dump_hierarchy(adb, serial))
            resolution = uiauto.resolve(target, nodes, screen)
            if resolution is None or resolution.node is None:
                raise ElementNotFound(
                    f"{target.name or 'target'} is not on screen; a TV cannot fall back to coordinates"
                )
            focused = next((node for node in nodes if node.focused), None)
            if focused is None:
                # Nothing focused yet -- one press establishes focus somewhere
                # and the next iteration navigates from there.
                self.key_event(adb, serial, DPAD_DOWN)
                continue
            if focused.bounds == resolution.node.bounds:
                self.key_event(adb, serial, DPAD_CENTER)
                return {"target": target.name, "strategy": resolution.strategy,
                        "x": resolution.point[0], "y": resolution.point[1]}
            self.key_event(adb, serial, self._step_toward(focused.center, resolution.node.center))
        raise VerificationError(
            f"focus did not reach {target.name or 'target'} within {self.MAX_FOCUS_STEPS} steps"
        )

    @staticmethod
    def _step_toward(origin: tuple[int, int], destination: tuple[int, int]) -> str:
        dx, dy = destination[0] - origin[0], destination[1] - origin[1]
        if abs(dy) >= abs(dx):
            return DPAD_DOWN if dy > 0 else DPAD_UP
        return DPAD_RIGHT if dx > 0 else DPAD_LEFT


def select_adapter(adb: AdbClientProtocol, serial: str) -> Adapter:
    """Pick the adapter matching what the device actually is.

    `ro.build.characteristics` is a comma-separated list ("tv",
    "tv,nosdcard", "phone", "default", ...). Each entry is stripped before
    comparison because `AdbClient._run` returns adb's stdout verbatim,
    trailing newline included -- so the final entry arrives as e.g. "tv\\n"
    and a bare `"tv" in blob.split(",")` misses it. That silently handed
    Android TVs the phone adapter, which sends `input tap`/`input swipe`
    coordinates to a device that only responds to DPAD key events, so
    scenarios appeared to run while doing nothing at all.

    Falls back to the generic adapter if the property can't be read: an
    unreadable characteristics string is not a reason to fail a whole run,
    and the generic adapter is right for the large majority of devices.
    """
    try:
        characteristics = adb.shell(serial, "getprop ro.build.characteristics").lower()
    except Exception:
        return AndroidAdapter()
    if "tv" in {entry.strip() for entry in characteristics.split(",")}:
        return AndroidTvAdapter()
    return AndroidAdapter()


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    at: float
    action: str
    kwargs: dict[str, Any] = field(default_factory=dict)
