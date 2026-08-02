from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

from .adb import AdbClientProtocol

_PACKAGE_RE = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z][\w]*)+$")
_ACTIVITY_RE = re.compile(r"^\.?[A-Za-z][\w.]*$")
_KEYCODE_RE = re.compile(r"^KEYCODE_[A-Z0-9_]+$")
_WM_SIZE_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")
# A display size override supersedes the physical panel size for input, so it
# is preferred when `wm size` reports one.
_OVERRIDE_SIZE_RE = re.compile(r"Override size:\s*(\d+)x(\d+)")
# `dumpsys window displays` spells the current rotation several ways depending
# on the Android version; all of them carry the Surface.ROTATION_* ordinal.
_ROTATION_RE = re.compile(r"(?:mCurrentRotation|cur=|rotation)[=\s]*(\d)\b")
_URI_RE = re.compile(r"^https://[A-Za-z0-9./:?=_&%-]+$")
_INPUT_TEXT_RE = re.compile(r"^[A-Za-z0-9 _.\-]+$")

HOME = "KEYCODE_HOME"
ENTER = "KEYCODE_ENTER"
BACK = "KEYCODE_BACK"
APP_SWITCH = "KEYCODE_APP_SWITCH"
DPAD_UP = "KEYCODE_DPAD_UP"
DPAD_DOWN = "KEYCODE_DPAD_DOWN"
DPAD_LEFT = "KEYCODE_DPAD_LEFT"
DPAD_RIGHT = "KEYCODE_DPAD_RIGHT"
DPAD_CENTER = "KEYCODE_DPAD_CENTER"

# What the TV family ships instead of the phone packages a scenario names. A
# module constant because two readers need exactly the same answer: the adapter
# that launches, and the profile that reports what will be launched.
TV_PACKAGE_MAP: dict[str, tuple[str, str | None]] = {
    "com.android.settings": ("com.android.tv.settings", ".MainSettings"),
    "com.google.android.youtube": ("com.google.android.youtube.tv", None),
}


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


def _require_input_text(text: str) -> str:
    """Validate text destined for `adb shell input text`.

    Deliberately narrow, for two separate reasons. It goes into a shell
    command, so the same injection concern that governs every other argument
    here applies. And `input text` is an ASCII keystroke injector: it does not
    reliably deliver non-ASCII on most devices, so accepting a Chinese query
    would silently type nothing rather than fail.
    """
    if not _INPUT_TEXT_RE.fullmatch(text):
        raise ValueError(f"Invalid or unsafe input text: {text!r}")
    return text


@dataclass(slots=True)
class Adapter(ABC):
    name: str = "adapter"

    # Logical package -> (the package this platform really has, activity override).
    # Scenarios are written against the phone's names; a platform that ships the
    # app under a different id declares the substitution here and every method
    # below applies it, rather than each one remembering to.
    #
    # Applying it per-method is what this replaces. Four overrides did it by
    # hand -- launch, stop, and two verifies -- so the correctness of a *new*
    # action depended on noticing that they did. Observed on a Chromecast: a
    # verify compared a scenario's `com.google.android.youtube` against the
    # `.tv` package that was really launched and failed every single run.
    # ClassVar so the dataclass leaves it alone: it is platform data shared by
    # every instance of an adapter, not per-instance state.
    PACKAGE_MAP: ClassVar[dict[str, tuple[str, str | None]]] = {}

    def mapped_package(self, package: str) -> str:
        """The package this platform actually drives for `package`."""
        mapped, _activity = self.PACKAGE_MAP.get(package, (package, None))
        return mapped

    def _launch_target(self, package: str, activity: str | None) -> tuple[str, str | None]:
        mapped, mapped_activity = self.PACKAGE_MAP.get(package, (package, None))
        return mapped, mapped_activity or activity

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


@dataclass(frozen=True, slots=True)
class Waits:
    """Every "how long do we give the device" number, in one place.

    These were six class attributes spread down the mixin, and only two of the
    behaviours they governed took an injectable `timeout`. That asymmetry was
    not cosmetic: it cost the test suite 61 of its 87 seconds, because zeroing
    the waits meant knowing four attribute names that nothing pointed to, and
    two of them were simply missed. A single object cannot be half-injected.

    Every value here is patience, not policy: how long the device is given
    before an answer is treated as final. All were calibrated on a Galaxy A55.

    Frozen because patience that changes mid-run makes two steps in the same
    scenario incomparable.
    """

    # A scenario step fires at its scripted time, which is a guess about how
    # long the app needs. Observed on a Galaxy A55: `subscriptions_feed_browse`
    # taps the Subscriptions tab at t=3.0s, and at that moment YouTube has
    # rendered its containers but not yet its bottom navigation -- so a
    # single-shot lookup found nothing and reported the selector as decayed
    # when the real problem was arriving early. Retrying briefly turns that
    # common case back into a match.
    #
    # `resolve_attempts` is a ceiling, not a plan: what actually stops the
    # retrying is `resolve_budget`, because an attempt's cost is not a constant.
    # Measured on a Galaxy A55, one `uiautomator dump`:
    #
    #     launcher, idle .............. 2.60s
    #     YouTube home feed ........... 2.73s
    #     watch page, video playing ... 3.01s median, 11.66s worst
    #
    # `uiautomator dump` waits for an idle UI and a playing video is never
    # idle, so the tail is long and unbounded. Three attempts plus two retry
    # delays is 11.0s at the *median* against TestRunner.adapter_action_timeout
    # of 10.0s -- so the retry that exists to rescue an early arrival instead
    # got the step killed, which is strictly worse than not retrying at all.
    resolve_attempts: int = 3
    resolve_retry: float = 1.0
    # Total for all attempts. Must stay under the runner's per-action timeout,
    # which a test asserts; the margin covers the tap that follows the lookup.
    resolve_budget: float = 7.0

    # A state check fired immediately after a tap is the same mistake as a
    # fixed-time tap, one layer up: the app needs a moment to re-render the
    # control, and how long depends on the device.
    state_timeout: float = 4.0
    state_poll: float = 0.5

    # Opening a video is asynchronous -- the app has to resolve, buffer and
    # begin rendering -- so sampling once at an arbitrary instant is inherently
    # flaky. Measured on a Galaxy A55: a search flow that had genuinely reached
    # the right video still read as not-playing two seconds after the tap,
    # because a livestream was still buffering.
    playback_timeout: float = 8.0
    playback_poll: float = 0.5

    # One `uiautomator dump`. The default was 15s, which alone exceeds the
    # runner's per-action timeout -- a single slow dump could take the step
    # past its budget with nothing to show. Sized to the observed worst case
    # (11.66s on a playing watch page) being cut off rather than waited out.
    dump_timeout: float = 6.0

    # How many DPAD presses focus may take to reach a target before the step is
    # called a failure. Bounded because a target focus cannot reach
    # (off-screen, unfocusable) would otherwise loop until the run ended.
    max_focus_steps: int = 12

    def __post_init__(self):
        # All of these sit on the path of an action the runner gives
        # `adapter_action_timeout` seconds to finish. A negative value would
        # not fail loudly, it would quietly turn a wait into no wait.
        for f in fields(self):
            if getattr(self, f.name) < 0:
                raise ValueError(f"{f.name} cannot be negative")

    @classmethod
    def instant(cls) -> "Waits":
        """No waiting, same number of attempts. For tests against a stub.

        A stub device does not change between reads, so the delays can only buy
        wall-clock -- but the attempt *counts* stay, because how many times a
        lookup retries is behaviour a test should still see. The waiting itself
        is covered separately, by tests that script a changing device.
        """
        return cls(resolve_retry=0.0, state_timeout=0.01, state_poll=0.0,
                   playback_timeout=0.01, playback_poll=0.0)


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
    """Carries the cost of the lookup that failed.

    A failed lookup is not a cheap one -- it is usually the *most* expensive
    kind, because the retry loop only gives up after spending its whole
    budget. Those dumps ran, took seconds, and burned CPU on the device whose
    CPU is being sampled at that moment.

    Without this the cost vanished at exactly the wrong time: `tap_element`
    reports `dumps`/`dump_seconds` in its return value, and raising skips the
    return. So a step that dumped three times and found nothing recorded no
    `ui_introspection` event, while a step that dumped once and succeeded
    recorded one -- the run that contaminated its own measurement most was the
    one that looked cheapest. `dumps` exists precisely because cost is the one
    thing a fake device cannot make you feel (see `DeviceAdb.dumps`).
    """

    def __init__(self, message: str, cost: dict | None = None):
        super().__init__(message)
        self.cost = dict(cost or {})


# Mixed into both adapters below. Kept separate from Adapter itself so the
# abstract interface stays the minimal set a plug-in must implement --
# these are all built from launch/tap/swipe plus a UI dump.
class ElementActionsMixin:
    # One object rather than six attributes, so patience is injected in one
    # move. An instance may shadow it: `AndroidAdapter(waits=Waits.instant())`.
    waits: Waits = Waits()

    def _resolve(self, adb: AdbClientProtocol, serial: str, target, screen=None,
                 cost: dict | None = None):
        """Locate `target`, reporting what the lookup cost the device.

        `cost` accumulates `dumps` and `dump_seconds`. It is threaded out
        rather than logged here because the adapter has no writer -- the runner
        turns it into an event, the same way it turns `strategy` into a
        `selector_fallback`.

        Why it is worth reporting at all: `uiautomator dump` takes one to three
        seconds on a real phone and is CPU-heavy, and it runs *concurrently
        with the collectors sampling that CPU*. A scenario with four
        tap_elements spends four to twelve dumps inside a thirteen-second
        timeline, so a meaningful share of what this tool reports as the app's
        CPU is its own instrumentation. Against a fake device that cost is
        invisible, which is why it went unmeasured until now.
        """
        from . import uiauto

        screen = screen or self.screen_size(adb, serial)
        resolution = None
        deadline = time.monotonic() + self.waits.resolve_budget
        for attempt in range(self.waits.resolve_attempts):
            started = time.monotonic()
            try:
                nodes = uiauto.parse_hierarchy(
                    uiauto.dump_hierarchy(adb, serial, timeout=self.waits.dump_timeout))
            except Exception:
                # A failed dump must not be fatal on its own: the coordinate
                # fallback is exactly as good as the behaviour that preceded
                # this module, so degrade to it rather than failing the step.
                nodes = []
            spent = time.monotonic() - started
            if cost is not None:
                # A dump that raised still cost the device the attempt.
                cost["dumps"] = cost.get("dumps", 0) + 1
                cost["dump_seconds"] = round(cost.get("dump_seconds", 0.0) + spent, 3)
            resolution = uiauto.resolve(target, nodes, screen)
            # Only a *selector* match ends the retry loop. Stopping at the
            # coordinate fallback would defeat the point, since the fallback
            # is available on the first attempt and every attempt after it.
            if resolution is not None and resolution.strategy != "coordinates":
                break
            # Retry only if another attempt can finish. The last dump's own
            # duration is the estimate, which makes this self-calibrating: on a
            # device where dumps are quick the full three attempts run, and on
            # one where they are slow a single attempt runs and falls back to
            # the coordinate -- which is a worse answer than a match and a far
            # better one than a step the runner kills mid-flight.
            if time.monotonic() + self.waits.resolve_retry + spent > deadline:
                if cost is not None:
                    cost["resolve_budget_spent"] = True
                break
            if attempt < self.waits.resolve_attempts - 1:
                time.sleep(self.waits.resolve_retry)
        return resolution, screen

    def tap_element(self, adb: AdbClientProtocol, serial: str, target, screen=None) -> dict:
        """Tap an element located by selector chain, coordinates last.

        Returns which strategy matched so the runner can record it: a step
        that quietly fell through to `coordinates` still worked, but it is
        the early warning that a selector has gone stale, and it is invisible
        unless reported.
        """
        cost: dict = {}
        resolution, _ = self._resolve(adb, serial, target, screen, cost)
        if resolution is None:
            raise ElementNotFound(
                f"no element matched {target.name or 'target'} and no coordinate fallback was given",
                cost,
            )
        x, y = resolution.point
        self.tap(adb, serial, x, y)
        return {"target": target.name, "strategy": resolution.strategy, "x": x, "y": y, **cost}

    def verify_foreground(self, adb: AdbClientProtocol, serial: str, package: str) -> dict:
        """Assert `package` is actually the app in front, and on screen.

        The keyguard half is not redundant. `mFocusedApp` goes on naming the
        app while the device is locked, so this check passed on a locked phone
        -- and so did `verify_playing`, because the audio really was still
        playing. Both strengths agreeing on a screen nobody could see is how a
        run gets marked verified while measuring a lockscreen.

        That is not hypothetical: on 2026-08-02 a Redmi Pad 2 with a 60-second
        screen timeout locked partway through a preflight session, and every
        target graded after that point was graded against SystemUI's 18
        lockscreen nodes. It read as "the selector table has collapsed on
        tablets" until the device was unlocked and 181 nodes came back.
        """
        from . import uiauto

        package = self.mapped_package(package)
        focus, locked = uiauto.window_state(adb, serial)
        if locked:
            raise VerificationError(
                "device is locked -- the app may be the foreground task but "
                "nothing on screen belongs to it")
        if focus is None:
            # Unreadable focus is not evidence of failure; saying so beats
            # failing a run because dumpsys was momentarily unavailable.
            return {"package": package, "verified": None}
        if focus[0] != package:
            raise VerificationError(f"expected {package} in foreground, found {focus[0]}")
        return {"package": package, "verified": True, "activity": focus[1]}

    def verify_element_state(self, adb: AdbClientProtocol, serial: str, target,
                             state: str = "selected", expected: bool = True,
                             timeout: float | None = None, screen=None) -> dict:
        """Assert an element's toggle state, not just that it was tapped.

        The gap this closes: `tap_element` proves a control was *found* and
        tapped, which is all a like or a subscribe currently gets. Whether the
        like registered is a different question, and the only evidence for it
        on the device is the node's own state -- so the check has to re-read
        the hierarchy and look.

        Deliberately refuses the coordinate fallback. Every other action here
        treats a pixel as an acceptable last resort, because tapping a
        remembered coordinate is still a tap. There is no equivalent for
        reading state: a coordinate carries no `selected` attribute, so an
        unmatched selector means "could not tell", never "not selected".

        Unreadable is reported as `verified: None`, in line with the other two
        checks -- a build that does not expose the attribute at all, or a
        signed-out account whose like button never changes, must not fail runs
        for a reason that has nothing to do with performance.
        """
        from . import uiauto

        if state not in uiauto.VERIFIABLE_STATES:
            raise ValueError(
                f"Unsupported element state {state!r}; expected one of {uiauto.VERIFIABLE_STATES}"
            )
        name = target.name or "target"
        cost: dict = {}
        deadline = time.monotonic() + (self.waits.state_timeout if timeout is None else timeout)
        node = None
        while True:
            resolution, screen = self._resolve(adb, serial, target, screen, cost)
            node = resolution.node if resolution is not None else None
            if node is not None and getattr(node, state) is expected:
                return {"target": target.name, "state": state, "expected": expected,
                        "verified": True, "strategy": resolution.strategy, **cost}
            if time.monotonic() >= deadline:
                break
            time.sleep(self.waits.state_poll)

        if node is None:
            return {"target": target.name, "state": state, "expected": expected,
                    "verified": None, **cost,
                    "detail": f"{name} was not located by any selector; state is unreadable"}
        raise VerificationError(
            f"expected {name}.{state} to be {expected}, found {getattr(node, state)}"
        )

    def verify_playing(self, adb: AdbClientProtocol, serial: str, package: str | None = None,
                       timeout: float | None = None) -> dict:
        """Assert media actually starts playing, waiting for it to begin.

        The strongest check available, and the only one that separates
        "search_and_play worked" from "the taps hit empty space and left the
        home feed on screen" -- a foreground check passes in both cases.

        Waits rather than samples, since the alternative is a check that
        fails on slow networks and passes on fast ones.
        """
        from . import uiauto

        package = self.mapped_package(package) if package else None
        deadline = time.monotonic() + (self.waits.playback_timeout if timeout is None else timeout)
        playing = None
        while True:
            playing = uiauto.is_playing(adb, serial, package)
            if playing:
                return {"verified": True}
            if time.monotonic() >= deadline:
                break
            time.sleep(self.waits.playback_poll)

        if playing is None:
            # No session to read at all -- "could not tell", not "not
            # playing". Failing here would fail runs for the wrong reason.
            return {"verified": None}
        raise VerificationError("expected active media playback, found none")


class AndroidAdapter(ElementActionsMixin, Adapter):
    """Generic AOSP adapter: drives the device with plain `adb shell input`/`am`/`monkey`
    commands only, no OEM-private APIs.

    OEM-specific hook point: subclass AndroidAdapter and override individual
    methods once a *real, observed* behavioral difference exists (e.g. a One UI
    dialog that must be dismissed before `am start` works, or a TV remote with no
    touchscreen that needs tap/swipe mapped to KEYCODE_DPAD_* instead). Don't
    pre-create empty subclasses before there's OEM logic to put in them.
    """

    # Class-level default, not set in __init__: AndroidTvAdapter deliberately
    # calls Adapter.__init__ directly, so anything only assigned here would be
    # missing on a TV. Instances shadow it with their own tuple. Per-adapter,
    # and the profile builds one per run, so it never outlives its device.
    _cached_panel: tuple[int, int] | None = None

    def __init__(self, waits: Waits | None = None):
        super().__init__("android")
        if waits is not None:
            self.waits = waits

    def launch_app(self, adb, serial, package, activity=None, data=None):
        package, activity = self._launch_target(package, activity)
        package = _require_package(package)
        if data:
            adb.shell(serial, f'am start -a android.intent.action.VIEW -d "{_require_uri(data)}" {package}')
        elif activity:
            adb.shell(serial, f"am start -n {package}/{_require_activity(activity)}")
        else:
            adb.shell(serial, f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def stop_app(self, adb, serial, package):
        adb.shell(serial, f"am force-stop {_require_package(self.mapped_package(package))}")

    def tap(self, adb, serial, x, y):
        adb.shell(serial, f"input tap {int(x)} {int(y)}")

    def swipe(self, adb, serial, x1, y1, x2, y2, duration_ms=300):
        adb.shell(serial, f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def key_event(self, adb, serial, keycode):
        adb.shell(serial, f"input keyevent {_require_keycode(keycode)}")

    def type_text(self, adb, serial, text):
        """Type into whatever field currently has focus.

        `input text` needs spaces escaped as `%s`; an unescaped space would
        be read as the end of the argument and only the first word would
        arrive.

        This is a keystroke injector, not a UI interaction: it depends on
        nothing being introspectable and works identically wherever a text
        field is focused, which is why it is a sound way to make a search
        flow deterministic.
        """
        adb.shell(serial, f"input text {_require_input_text(text).replace(' ', '%s')}")

    def screen_size(self, adb, serial):
        """The coordinate space taps and swipes actually land in.

        Three things make this more than reading one number:

        `wm size` reports `Physical size` (the panel) and, when a display size
        override is set, an `Override size` that supersedes it. Input lands in
        the override, so it wins when present.

        Neither line changes with rotation -- both stay portrait-shaped on a
        rotated device. A landscape tablet would therefore have every
        fractional coordinate computed against portrait dimensions: a tap
        meant for the top-right corner lands mid-left instead. Rotation is
        read separately and the dimensions swapped for the landscape cases.

        Rotation constants are Surface.ROTATION_0/90/180/270; 90 and 270 are
        the landscape ones.
        """
        width, height = self._panel_size(adb, serial)
        if self._rotation(adb, serial) in (1, 3):
            width, height = height, width
        return width, height

    def _panel_size(self, adb, serial) -> tuple[int, int]:
        """The unrotated dimensions, read once per adapter.

        Cached because neither line `wm size` prints can change during a run --
        a panel does not resize, and a display-size override is set by hand.
        Rotation is the only part that moves, so it stays uncached above.

        Worth caching because this is on the path of *every* element action:
        `_resolve` needs the coordinate space, so a scenario with four
        tap_elements asked the device for its unchanging dimensions four times.
        Cheap next to a UI dump, but it is the kind of cost that is invisible
        against a fake device and real against a phone.
        """
        if self._cached_panel is None:
            output = adb.shell(serial, "wm size")
            match = _OVERRIDE_SIZE_RE.search(output) or _WM_SIZE_RE.search(output)
            if not match:
                raise ValueError("Unable to parse screen size")
            self._cached_panel = (int(match.group(1)), int(match.group(2)))
        return self._cached_panel

    @staticmethod
    def _rotation(adb, serial) -> int:
        """Current display rotation, or 0 if it cannot be read.

        Assuming portrait on an unreadable rotation matches how every device
        behaved before rotation was considered at all, so a failure here can
        only leave things as they were rather than make them worse.
        """
        try:
            output = adb.shell(serial, "dumpsys window displays")
        except Exception:
            return 0
        match = _ROTATION_RE.search(output or "")
        return int(match.group(1)) if match else 0


class AndroidTvAdapter(AndroidAdapter):
    """Maps phone-oriented scenarios onto Android TV packages and DPAD input."""

    PACKAGE_MAP = TV_PACKAGE_MAP

    def __init__(self, waits: Waits | None = None):
        Adapter.__init__(self, "android-tv")
        if waits is not None:
            self.waits = waits

    def tap(self, adb, serial, x, y):
        self.key_event(adb, serial, DPAD_CENTER)

    def swipe(self, adb, serial, x1, y1, x2, y2, duration_ms=300):
        dx, dy = int(x2) - int(x1), int(y2) - int(y1)
        if abs(dy) >= abs(dx):
            keycode = DPAD_DOWN if dy < 0 else DPAD_UP
        else:
            keycode = DPAD_RIGHT if dx < 0 else DPAD_LEFT
        self.key_event(adb, serial, keycode)

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
        for _ in range(self.waits.max_focus_steps):
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
            f"focus did not reach {target.name or 'target'} "
            f"within {self.waits.max_focus_steps} steps"
        )

    @staticmethod
    def _step_toward(origin: tuple[int, int], destination: tuple[int, int]) -> str:
        dx, dy = destination[0] - origin[0], destination[1] - origin[1]
        if abs(dy) >= abs(dx):
            return DPAD_DOWN if dy > 0 else DPAD_UP
        return DPAD_RIGHT if dx > 0 else DPAD_LEFT


def is_tv(adb: AdbClientProtocol, serial: str) -> bool:
    """Whether this device is an Android TV, by its own report.

    `ro.build.characteristics` is a comma-separated list ("tv",
    "tv,nosdcard", "phone", "default", ...). Each entry is stripped before
    comparison because `AdbClient._run` returns adb's stdout verbatim,
    trailing newline included -- so the final entry arrives as e.g. "tv\\n"
    and a bare `"tv" in blob.split(",")` misses it. That silently handed
    Android TVs the phone adapter, which sends `input tap`/`input swipe`
    coordinates to a device that only responds to DPAD key events, so
    scenarios appeared to run while doing nothing at all.

    An unreadable property answers False: it is not a reason to fail a run,
    and the generic phone family is right for the large majority of devices.

    The one place the platform is probed. `select_adapter` here and
    `profiles.select_profile` both come through it, so the two cannot
    disagree about what a device is.
    """
    try:
        characteristics = adb.shell(serial, "getprop ro.build.characteristics").lower()
    except Exception:
        return False
    return "tv" in {entry.strip() for entry in characteristics.split(",")}


def select_adapter(adb: AdbClientProtocol, serial: str) -> Adapter:
    """The adapter for this device.

    Kept for callers that need only the adapter. Anything that also needs
    collectors, a target table or a package name should ask
    `profiles.select_profile` instead -- those vary by platform together, and
    picking them separately is how a TV ends up driven by a TV adapter while
    being measured against a phone's assumptions.
    """
    return AndroidTvAdapter() if is_tv(adb, serial) else AndroidAdapter()


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    at: float
    action: str
    kwargs: dict[str, Any] = field(default_factory=dict)
