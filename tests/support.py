"""Shared test doubles for the one seam the device layer has.

Everything device-facing goes through `adb.shell(serial, command, timeout)`,
which is why this whole suite runs without a device. Two doubles cover it, and
they are kept apart on purpose:

- `RecordingAdb` asserts what was *sent*. Its job is the command string.
- `DeviceAdb` supplies what the device *replies*. Its job is the screen.

Merging them would make every test configure both halves when it only cares
about one. What is worth sharing is the data and the strictness policy, not the
class.

Neither belongs in a test of the *parsers*. `uiauto.current_focus`,
`is_playing` and `dump_hierarchy` are fed deliberately malformed dumpsys text,
and a double that models a well-behaved device is the wrong shape for that --
those tests keep their own local responders, on purpose.

**Strict by default.** An unlisted command raises. Before this module there
were two `FakeAdb` classes that looked alike and behaved oppositely -- the dict
ones raised `KeyError`, the if-chain ones returned `""` -- so whether a typo'd
command failed a test depended on which file the test lived in. Permissiveness
is now one explicit argument (`allow_unknown=True`) rather than a property of
where you happened to be.
"""

from __future__ import annotations

from unittest.mock import patch

SCREEN = (1080, 2340)
YOUTUBE = "com.google.android.youtube"

# A phone home feed: one search icon, one bottom-nav button, three feed rows.
# Enough for content-desc, resource-id and structural selection to all be
# exercised against the same screen.
HOME_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
 <node class="android.widget.FrameLayout" bounds="[0,0][1080,2340]" package="com.google.android.youtube">
  <node class="android.widget.ImageView" resource-id="com.google.android.youtube:id/search_button"
        content-desc="Search" clickable="true" bounds="[940,80][1040,180]"/>
  <node class="androidx.recyclerview.widget.RecyclerView"
        resource-id="com.google.android.youtube:id/results" bounds="[0,200][1080,2200]">
   <node class="android.view.ViewGroup" clickable="true" bounds="[0,200][1080,800]" content-desc="Video A"/>
   <node class="android.view.ViewGroup" clickable="true" bounds="[0,800][1080,1400]" content-desc="Video B"/>
   <node class="android.view.ViewGroup" clickable="true" bounds="[0,1400][1080,2000]" content-desc="Video C"/>
  </node>
  <node class="android.widget.Button" text="Like" clickable="true" bounds="[100,2250][200,2320]" enabled="false"/>
  <node class="android.widget.Button" content-desc="Shorts" clickable="true" focused="true"
        bounds="[600,2250][700,2320]"/>
 </node>
</hierarchy>"""

EMPTY_SCREEN = "<hierarchy></hierarchy>"

# Controls that report their toggle state, for verify_element_state. Whether a
# real build does this is a property of that build -- see describe_clickables.
TOGGLES = """<hierarchy>
 <node class="android.widget.Button" content-desc="喜歡這部影片" clickable="true"
       selected="true" bounds="[100,2200][200,2300]"/>
 <node class="android.widget.CheckBox" content-desc="Autoplay" clickable="true"
       checked="true" bounds="[300,2200][400,2300]"/>
 <node class="android.widget.Button" content-desc="訂閱" clickable="true"
       bounds="[500,2200][600,2300]"/>
</hierarchy>"""

# What the collectors parse. Values are arbitrary but fixed, so a test can
# assert on the parsed numbers without restating the raw output.
METRIC_REPLIES = {
    "dumpsys cpuinfo": "1.0% TOTAL: 1.0% user + 0.0% kernel",
    "cat /proc/meminfo": "MemTotal: 100 kB\nMemAvailable: 50 kB\n",
    "dumpsys battery": " level: 50\n temperature: 300\n",
}

PLAYING = 3      # android.media.session.PlaybackState.STATE_PLAYING
PAUSED = 2


class RecordingAdb:
    """Records every call and returns one canned reply. For asserting commands."""

    def __init__(self, response: str = ""):
        self.response = response
        self.calls: list[tuple[str, str, int]] = []

    def shell(self, serial, command, timeout=10):
        self.calls.append((serial, command, timeout))
        return self.response

    @property
    def commands(self) -> list[str]:
        return [command for _serial, command, _timeout in self.calls]


class DeviceAdb:
    """A device that answers the commands the layer under test issues.

    Composed rather than subclassed, because the axes are independent: a screen
    (`hierarchy`), a foreground app (`focus_package`), a playback state
    (`playback_state`, or `playback_states` for one that changes), collector
    output (`metrics`), and failure switches (`dump_fails`).

    `dumps` counts hierarchy reads. It is exposed because the cost is real and
    otherwise invisible: `uiautomator dump` takes seconds on a device and
    competes with the very CPU the collectors are sampling, while a stub
    answers instantly. A test can assert an action does not dump more than it
    must.
    """

    def __init__(self, *, hierarchy: str | None = HOME_FEED, focus_package: str | None = YOUTUBE,
                 playback_state: int | None = PLAYING, playback_states: list[int | None] | None = None,
                 screen: tuple[int, int] = SCREEN, metrics: bool = False,
                 characteristics: str = "phone", dump_fails: bool | str = False,
                 replies: dict[str, str] | None = None, fail_first: dict[str, int] | None = None,
                 allow_unknown: bool = False):
        self.hierarchy = hierarchy
        self.focus_package = focus_package
        self.playback_state = playback_state
        self.playback_states = list(playback_states) if playback_states is not None else None
        self.screen = screen
        self.characteristics = characteristics
        self.dump_fails = dump_fails
        self.replies = dict(replies or {})
        if metrics:
            self.replies.update(METRIC_REPLIES)
        # command prefix -> how many of its first calls raise. A device that
        # fails once and then works is a condition this system is built to
        # survive (a campaign keeps collecting, a dump degrades to
        # coordinates), so expressing it needs to be easier than subclassing.
        self.fail_first = dict(fail_first or {})
        self.allow_unknown = allow_unknown
        self.commands: list[str] = []
        self.dumps = 0
        self.playback_reads = 0

    # -- the seam ----------------------------------------------------------
    def devices(self):
        from autoperf.models import Device

        return [Device("SERIAL1", "device", "Pixel", "pixel")]

    def shell(self, serial, command, timeout=10):
        self.commands.append(command)
        for prefix, remaining in self.fail_first.items():
            if remaining > 0 and command.startswith(prefix):
                self.fail_first[prefix] = remaining - 1
                raise RuntimeError(f"device not responding to {prefix!r}")
        if command in self.replies:
            return self.replies[command]
        for prefix, handler in (
            ("uiautomator", self._dump),
            ("cat /sdcard/", self._hierarchy),
            ("wm size", self._size),
            ("dumpsys window", self._window),
            ("dumpsys media_session", self._media_session),
            ("getprop ro.build.characteristics", lambda: f"{self.characteristics}\n"),
        ):
            if command.startswith(prefix):
                return handler()
        # Commands that only act. Listed rather than caught by a bare default,
        # so an unexpected *read* still fails loudly.
        if command.startswith(("input ", "am ", "monkey ", "screencap", "screenrecord")):
            return ""
        if self.allow_unknown:
            return ""
        raise KeyError(
            f"DeviceAdb was asked {command!r}, which it has no reply for. Add it to "
            f"`replies=` if the code under test should be issuing it, or pass "
            f"allow_unknown=True if this test genuinely does not care."
        )

    # -- handlers ----------------------------------------------------------
    def _dump(self):
        self.dumps += 1
        if self.dump_fails:
            reason = self.dump_fails if isinstance(self.dump_fails, str) else "could not get idle state"
            raise RuntimeError(reason)
        return "UI hierchary dumped to: /sdcard/window_dump.xml"

    def _hierarchy(self):
        return self.hierarchy or ""

    def _size(self):
        return f"Physical size: {self.screen[0]}x{self.screen[1]}\n"

    def _window(self):
        if self.focus_package is None:
            return "nothing useful"
        return f"  mCurrentFocus=Window{{a b {self.focus_package}/{self.focus_package}.Main}}"

    def _media_session(self):
        self.playback_reads += 1
        state = self.playback_state
        if self.playback_states is not None:
            state = self.playback_states.pop(0) if self.playback_states else state
            self.playback_state = state
        if state is None:
            return ""
        return f"package={YOUTUBE}\n state=PlaybackState {{state={state}}}"

    # -- convenience -------------------------------------------------------
    @property
    def taps(self) -> list[str]:
        return [c for c in self.commands if c.startswith("input tap")]


class NoWaits:
    """Mixin: no device waiting for the duration of each test.

    One patch, because patience is one object. It used to be five attribute
    names that nothing pointed to, and the two that were missed cost the suite
    24 of its 87 seconds -- which is the argument for `Waits` in a sentence.

    Attempt *counts* are untouched: how many times a lookup retries is
    behaviour a test should still see. Prefer an injected `timeout=` where the
    method offers one; this is for the paths that reach a wait through scenario
    kwargs or a CLI, where there is no argument to pass.
    """

    def setUp(self):
        super().setUp()
        from autoperf.adapters import ElementActionsMixin, Waits

        patcher = patch.object(ElementActionsMixin, "waits", Waits.instant())
        patcher.start()
        self.addCleanup(patcher.stop)
