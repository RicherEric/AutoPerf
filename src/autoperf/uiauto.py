"""Locate on-screen elements by identity instead of by coordinate.

`adb shell input tap X Y` succeeds whenever X,Y is on the screen, whether or
not anything is there. That made every coordinate-driven scenario step fail
*silently*: the runner saw no exception, recorded "adapter_action completed",
and the run finished green while the app sat untouched on whatever screen it
happened to be showing. For performance work that is worse than no test at
all -- the metrics are real numbers measuring the wrong thing, and a UI change
that breaks the taps shows up as an apparent efficiency improvement.

This module wraps `uiautomator dump`, Android's own accessibility-tree
export, so a step can say "the search button" rather than "92% across, 6%
down" and *fail loudly* when that button isn't there.

Everything here is pure parsing and matching over an XML string, so it is
testable without a device; the adb round-trip is confined to `dump_hierarchy`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree

from .adb import AdbClientProtocol

# `uiautomator dump` writes to a file and prints the path; dumping straight to
# stdout is possible but interleaves with adb's own chatter on some builds, so
# the file round-trip is the reliable form.
_DUMP_PATH = "/sdcard/window_dump.xml"

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


class UiDumpError(RuntimeError):
    """The device produced no usable UI hierarchy."""


@dataclass(frozen=True, slots=True)
class Node:
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    class_name: str = ""
    package: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    clickable: bool = False
    focused: bool = False
    enabled: bool = True
    # Toggle state, for asserting that an action landed rather than merely
    # that its control was tapped. Android exposes two flavours and apps pick
    # between them freely -- `selected` for highlight-style toggles, `checked`
    # for checkbox-style ones -- so both are read and the caller says which
    # one it means.
    selected: bool = False
    checked: bool = False

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def area(self) -> int:
        left, top, right, bottom = self.bounds
        return max(0, right - left) * max(0, bottom - top)


@dataclass(frozen=True, slots=True)
class Selector:
    """How to find one element. Every supplied field must match.

    Stability, best first -- this is the order selector chains should be
    written in:

    1. `content_desc` -- the accessibility label. Changing it breaks screen
       readers, which app developers are under real pressure not to do, so it
       moves far less often than internal ids. Can be translated, though.
    2. `resource_id` -- stable *within* an app version and meaningless
       across them: it is an internal implementation detail the app's authors
       have no reason to keep. Accepts either the bare id ("search_button")
       or the fully qualified form
       ("com.google.android.youtube:id/search_button"), since which one a
       dump reports varies by Android version.
    3. `class_name` + `index` -- structural: "the third clickable row in the
       list". Survives renames entirely, and for feed-shaped screens it also
       matches the actual intent ("open some video", not "open that exact
       one").
    4. `text` -- breaks on the first language change. A hint, not an anchor.

    `index` picks the index-th match in document order rather than the
    smallest one, which is what makes structural selection possible.
    """

    resource_id: str | None = None
    text: str | None = None
    content_desc: str | None = None
    class_name: str | None = None
    clickable: bool | None = None
    index: int | None = None
    min_area: int | None = None
    exact: bool = False
    description: str = ""

    def matches(self, node: Node) -> bool:
        if self.resource_id is not None and not _id_matches(self.resource_id, node.resource_id):
            return False
        if self.text is not None and not _text_matches(self.text, node.text, self.exact):
            return False
        if self.content_desc is not None and not _text_matches(
                self.content_desc, node.content_desc, self.exact):
            return False
        if self.class_name is not None and self.class_name not in node.class_name:
            return False
        if self.clickable is not None and node.clickable is not self.clickable:
            return False
        if self.min_area is not None and node.area < self.min_area:
            return False
        return True


@dataclass(frozen=True, slots=True)
class Target:
    """A selector chain plus the coordinate that used to be hardcoded.

    Real resource-ids change between app versions and cannot be verified
    without the app in front of you, so a wrong guess must not break a
    scenario that previously worked. Each selector is tried in order and the
    fractional coordinate is the last resort -- identical to the behaviour
    before this module existed. Which strategy actually matched is reported
    back to the caller, so "we fell back to coordinates again" is visible in
    the run's events instead of being indistinguishable from success.
    """

    selectors: tuple[Selector, ...] = ()
    fallback: tuple[float, float] | None = None
    name: str = ""
    # True for controls the app draws itself, which are therefore absent from
    # the accessibility tree on every device and build measured so far. For
    # those, reaching the coordinate is the intended path, not a decayed
    # selector -- so the runner must not treat it as a reason to distrust the
    # run. Every other target falling back means the table has gone stale
    # against this device, and that *does* invalidate the numbers: the tap
    # landed wherever a different screen used to put the control, and
    # `input tap` reports success on empty space.
    coordinate_only: bool = False


@dataclass(frozen=True, slots=True)
class Resolution:
    point: tuple[int, int]
    strategy: str          # "resource_id" | "content_desc" | "text" | "class" | "coordinates"
    selector_index: int | None = None
    node: Node | None = field(default=None, compare=False)


# Node attributes an assertion may be written against. Deliberately just the
# two toggles: `enabled` is *not* here because `find` already skips disabled
# nodes, so `expected=False` could never match and the assertion would look
# supported while being unwritable.
VERIFIABLE_STATES = ("selected", "checked")


# Labels at or below this length must match a node's whole label rather than
# appearing anywhere inside it. Substring matching is the right default --
# YouTube appends state and counts to its labels, so "喜歡這部影片" has to match
# "喜歡這部影片，共 1.2 萬個喜歡" -- but it is actively dangerous for very short
# labels. Observed on a Galaxy A55: the Library tab's "你" matched inside a
# video title ("沒有聽完是你的損失"), so the selector confidently resolved to that
# video's overflow-menu button and reported a successful match. A wrong match
# is worse than no match: it taps the wrong control and looks like it worked.
SHORT_LABEL_LENGTH = 2


def _text_matches(wanted: str, actual: str, exact: bool) -> bool:
    if not wanted:
        return True
    wanted, actual = wanted.lower(), (actual or "").lower()
    if exact or len(wanted) <= SHORT_LABEL_LENGTH:
        return wanted == actual
    return wanted in actual


def _id_matches(wanted: str, actual: str) -> bool:
    if not actual:
        return False
    if wanted == actual:
        return True
    # Accept a bare id against a fully-qualified one and vice versa.
    return actual.rsplit("/", 1)[-1] == wanted.rsplit("/", 1)[-1]


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    match = _BOUNDS_RE.fullmatch(raw.strip()) if raw else None
    if not match:
        return (0, 0, 0, 0)
    left, top, right, bottom = (int(value) for value in match.groups())
    return (left, top, right, bottom)


def parse_hierarchy(xml: str) -> list[Node]:
    """Flatten a `uiautomator dump` XML tree into nodes.

    Returns an empty list rather than raising on unparseable input: a
    truncated dump is a device hiccup, and the caller's fallback chain is a
    better response to it than aborting the whole run.
    """
    if not xml or not xml.strip():
        return []
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    nodes: list[Node] = []
    for element in root.iter("node"):
        attrib = element.attrib
        nodes.append(Node(
            resource_id=attrib.get("resource-id", ""),
            text=attrib.get("text", ""),
            content_desc=attrib.get("content-desc", ""),
            class_name=attrib.get("class", ""),
            package=attrib.get("package", ""),
            bounds=_parse_bounds(attrib.get("bounds", "")),
            clickable=attrib.get("clickable") == "true",
            focused=attrib.get("focused") == "true",
            enabled=attrib.get("enabled", "true") == "true",
            selected=attrib.get("selected") == "true",
            checked=attrib.get("checked") == "true",
        ))
    return nodes


def find(nodes: list[Node], selector: Selector) -> Node | None:
    """The best match for `selector`, or None.

    With no `index`, ties break toward the *smallest* matching node. Android
    hierarchies nest heavily, so a container usually matches the same criteria
    as the button inside it; the smallest match is the most specific one, and
    tapping a big container's centre often lands on padding rather than on
    anything interactive.

    With an `index`, matches are taken in document order instead -- that is
    what "the third row in the list" means, and re-sorting by size would
    scramble it.
    """
    matches = [node for node in nodes if node.enabled and selector.matches(node)]
    if not matches:
        return None
    if selector.index is not None:
        if selector.index >= len(matches):
            return None
        return matches[selector.index]
    return min(matches, key=lambda node: (node.area or 1 << 30))


def _strategy_of(selector: Selector) -> str:
    # Reported in run events, so the ordering here mirrors the stability
    # ranking in Selector's docstring -- seeing "text" or "coordinates" in a
    # run's events is the signal that a selector chain has decayed.
    if selector.content_desc is not None:
        return "content_desc"
    if selector.resource_id is not None:
        return "resource_id"
    # Positional selection is structural whether or not a class narrows it --
    # "the third clickable row" is the same idea either way.
    if selector.index is not None:
        return "structural"
    if selector.class_name is not None:
        return "class"
    if selector.text is not None:
        return "text"
    return "unknown"


def resolve(target: Target, nodes: list[Node], screen: tuple[int, int]) -> Resolution | None:
    """Walk `target`'s selector chain, then its coordinate fallback."""
    for index, selector in enumerate(target.selectors):
        node = find(nodes, selector)
        if node is not None:
            return Resolution(node.center, _strategy_of(selector), index, node)
    if target.fallback is not None:
        width, height = screen
        fx, fy = target.fallback
        return Resolution((round(width * fx), round(height * fy)), "coordinates")
    return None


def dump_hierarchy(adb: AdbClientProtocol, serial: str, timeout: float = 6.0) -> str:
    """Capture the device's current UI hierarchy as XML.

    The most expensive thing this project asks a device to do, by a factor of
    twenty. Measured on a Galaxy A55, Android 15, median of five:

        launcher, idle .............. 2.60s
        YouTube home feed ........... 2.73s
        watch page, video playing ... 3.01s median, 11.66s worst

    Every other read is around 0.12s. `uiautomator dump` waits for an idle UI
    and a playing video is never idle, which is where the long tail comes from.

    The default timeout is 6s, not the 15s it was: 15 exceeds the runner's whole
    per-action budget, so a single slow dump could take a step past it with
    nothing to show. Being cut off and falling back to a coordinate is a worse
    answer than a match and a much better one than a killed step.

    Three cheaper-looking alternatives were measured and rejected:

        uiautomator dump --compressed   6% faster, and 27 nodes instead of 72 --
                                        it prunes the views selectors need
        shell ... /dev/tty              returns no XML at all, only a status line
        exec-out ... /dev/tty           2% faster, and appends a status line
                                        after the closing tag, so it does not parse

    All three are within 12% of each other because the cost is `uiautomator
    dump` waiting for idle, not the transport. The only optimisation that
    matters is dumping less often.
    """
    output = adb.shell(serial, f"uiautomator dump {_DUMP_PATH}", timeout=timeout)
    if "ERROR" in output.upper() and "dumped" not in output.lower():
        raise UiDumpError(output.strip() or "uiautomator dump failed")
    xml = adb.shell(serial, f"cat {_DUMP_PATH}", timeout=timeout)
    if "<hierarchy" not in xml:
        raise UiDumpError("device returned no UI hierarchy")
    return xml


_FOCUS_RE = re.compile(r"(?:mCurrentFocus|mFocusedApp)=.*?(?:\s|\{)([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)")


# `dumpsys window` states the keyguard as `mDreamingLockscreen=true|false`.
_LOCKSCREEN_RE = re.compile(r"mDreamingLockscreen=(true|false)")


def window_state(adb: AdbClientProtocol,
                 serial: str) -> tuple[tuple[str, str] | None, bool | None]:
    """Focused app and lock state, from a single `dumpsys window`.

    Both answers come off the same output because the read costs 113ms on a
    Galaxy A55 and 240ms on a Redmi Pad 2, and asking twice for two fields of
    one dump is exactly the kind of cost that is free against a fake device
    and real against a phone.

    They have to be read together for a second reason. `mFocusedApp` keeps
    naming the app while the keyguard is up, so focus alone answers "is my app
    the foreground task" when the question a measurement needs answered is "is
    my app on the screen". Measured 2026-08-02 on a locked A55: YouTube was
    still `mFocusedApp`, still had `PlaybackState == 3` from its background
    audio, and both verifications passed while the screen showed the
    lockscreen.

    Lock state is tri-state on purpose: `None` means the dump did not say, and
    an unreadable keyguard must not fail a run any more than an unreadable
    focus does.
    """
    try:
        output = adb.shell(serial, "dumpsys window")
    except Exception:
        return None, None
    focus_match = _FOCUS_RE.search(output)
    focus = (focus_match.group(1), focus_match.group(2)) if focus_match else None
    lock_match = _LOCKSCREEN_RE.search(output)
    locked = (lock_match.group(1) == "true") if lock_match else None
    return focus, locked


def current_focus(adb: AdbClientProtocol, serial: str) -> tuple[str, str] | None:
    """The package/activity currently in front, or None if it can't be read.

    Parsed from `dumpsys window`, which reports both `mCurrentFocus` (the
    focused window) and `mFocusedApp` (the focused activity).

    **This does not mean the app is visible.** See `window_state`, which reads
    the keyguard from the same output -- callers deciding whether a screen is
    worth measuring want that one.
    """
    return window_state(adb, serial)[0]


# `dumpsys media_session` renders the inner state either as a bare number or,
# on current Android, as a named constant carrying the number:
#
#     state=PlaybackState {state=PLAYING(3), position=37, ...}   <- Android 14/15
#     state=PlaybackState {state=3, position=37, ...}            <- older
#
# Matching only the bare form made is_playing() return "unknown" on every
# modern device, which meant verify_playing could never fail -- the strongest
# check in the system quietly verified nothing. Observed on a Chromecast
# running Android 14.
_PLAYBACK_STATE_RE = re.compile(
    r"state=PlaybackState\s*\{[^}]*?\bstate=(?:[A-Za-z_]+\((\d+)\)|(\d+))"
)
_PLAYBACK_NUM_RE = re.compile(r"\bstate=(?:[A-Za-z_]+\((\d+)\)|(\d+))")


def _state_number(match: re.Match) -> int:
    """The numeric state from whichever alternative the pattern matched."""
    return int(next(group for group in match.groups() if group is not None))

# android.media.session.PlaybackState
STATE_PLAYING = 3


def is_playing(adb: AdbClientProtocol, serial: str, package: str | None = None) -> bool | None:
    """Whether the device currently has active media playback.

    A far stronger check than "the app is in the foreground": it is the only
    thing that actually distinguishes `search_and_play` having worked from
    `search_and_play` having tapped four times into empty space and left
    YouTube sitting on its home feed. Foreground checks pass in both cases.

    Returns None when the state cannot be read at all, so callers can tell
    "not playing" apart from "couldn't tell" -- treating an unreadable
    dumpsys as a failed assertion would fail runs for the wrong reason.
    """
    try:
        output = adb.shell(serial, "dumpsys media_session")
    except Exception:
        return None
    if not output.strip():
        return None
    if package:
        # Narrow to the section describing this package's session, so another
        # app's background music can't be mistaken for our video playing.
        index = output.find(package)
        if index == -1:
            return False
        output = output[index:index + 4000]
    match = _PLAYBACK_STATE_RE.search(output) or _PLAYBACK_NUM_RE.search(output)
    if not match:
        return None
    return _state_number(match) == STATE_PLAYING


_VERSION_NAME_RE = re.compile(r"versionName=(\S+)")
_VERSION_CODE_RE = re.compile(r"versionCode=(\d+)")


def package_version(adb: AdbClientProtocol, serial: str, package: str) -> dict | None:
    """The installed build of `package`, or None if it can't be read.

    Recorded per run because a comparison across two different app builds
    measures the app's change, not the device's. Without this, a silent
    background update between a baseline and its candidate is
    indistinguishable from a genuine regression -- and it is the more likely
    explanation of the two.

    It also bounds how fast selectors decay: pin the app version, as
    performance regression testing requires anyway, and the UI cannot move
    until the version is deliberately bumped.
    """
    try:
        output = adb.shell(serial, f"dumpsys package {package}")
    except Exception:
        return None
    name = _VERSION_NAME_RE.search(output or "")
    code = _VERSION_CODE_RE.search(output or "")
    if not name and not code:
        return None
    return {
        "package": package,
        "version_name": name.group(1) if name else None,
        "version_code": int(code.group(1)) if code else None,
    }


def describe_clickables(nodes: list[Node], limit: int = 60) -> list[dict]:
    """Interactive elements, for capturing real selectors from a device.

    The resource-ids a scenario should use can only be learned by looking at
    the app itself -- they differ per app version and are not published --
    so this backs the `autoperf ui-dump` command.

    `selected` and `checked` are included for the same reason: whether a build
    reports its toggle state at all is a property of that build, and
    `verify_element_state` is only worth writing against a control that does.
    Dumping the screen with the toggle pressed answers that.
    """
    seen: set[tuple[str, str, str]] = set()
    described = []
    for node in nodes:
        if not (node.clickable or node.resource_id):
            continue
        key = (node.resource_id, node.text, node.content_desc)
        if key in seen or key == ("", "", ""):
            continue
        seen.add(key)
        described.append({
            "resource_id": node.resource_id,
            "text": node.text,
            "content_desc": node.content_desc,
            "class": node.class_name,
            "clickable": node.clickable,
            "selected": node.selected,
            "checked": node.checked,
            "center": list(node.center),
            "bounds": list(node.bounds),
        })
        if len(described) >= limit:
            break
    return described
