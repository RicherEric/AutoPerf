"""Save what a real device actually said, so tests stop guessing.

Every fixture of device output in this suite was hand-written, which makes each
one a guess about a device nobody had in front of them. The record of what that
costs is the git log: `verify_playing`'s parser matched only a format no modern
device emits, so the strongest check in the system quietly verified nothing
while 305 tests stayed green; nineteen selector labels were wrong; the search
flow never typed anything and seven scenarios passed anyway. Every one of those
was found by running against hardware, and none by a test.

A capture is the same text a test needs, taken from a device and stored with
enough provenance to be worth trusting later: which model, which Android, which
app build, and when. Without the provenance it is just another literal with an
unknown origin -- the problem it was meant to solve.

What this deliberately does not do is assert anything. A capture is evidence;
what to conclude from it belongs in a test.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .adb import AdbClientProtocol

# Test data, so it lives with the tests rather than in the package. A capture's
# whole purpose is to be read by one.
CAPTURES_ROOT = Path(__file__).resolve().parents[2] / "tests" / "captures"

MANIFEST = "manifest.json"

# Measured on a Galaxy A55: a dump costs 2.6s on an idle screen and up to 11.7s
# on a playing watch page, because `uiautomator dump` waits for an idle UI.
DUMP_TIMEOUT = 30.0

# The reads a test might need from the same moment. The hierarchy is the one
# selectors are resolved against; the other two back the parsers that decide
# whether an app is in front and whether anything is playing -- the two that
# were wrong on real hardware while their hand-written fixtures said otherwise.
READS: dict[str, str] = {
    "window": "dumpsys window",
    "media_session": "dumpsys media_session",
}

_UNSAFE = re.compile(r"[^a-z0-9_]+")


def _slug(text: str) -> str:
    return _UNSAFE.sub("_", text.strip().lower()).strip("_")


def device_fingerprint(adb: AdbClientProtocol, serial: str) -> dict:
    """What this device is, as it describes itself.

    Recorded per capture rather than per directory because a device gets
    updated: the same phone on Android 15 and on Android 16 can publish
    different hierarchies for the same screen, and a capture whose Android
    version is unknown cannot be trusted to still describe anything.
    """
    def prop(name: str) -> str:
        try:
            return adb.shell(serial, f"getprop {name}").strip()
        except Exception:
            return ""

    return {
        "model": prop("ro.product.model"),
        "android_release": prop("ro.build.version.release"),
        "sdk": prop("ro.build.version.sdk"),
        "characteristics": prop("ro.build.characteristics"),
    }


def device_slug(fingerprint: dict) -> str:
    """A directory name a human can recognise: `sm_a5560_android15`."""
    model = _slug(fingerprint.get("model") or "unknown")
    release = _slug(fingerprint.get("android_release") or "unknown")
    return f"{model}_android{release}"


def app_fingerprint(adb: AdbClientProtocol, serial: str, package: str) -> dict:
    """The installed build of `package`, so a capture is pinned to an app version.

    A selector table is only stable for as long as the app version is: the ids
    and labels are internal details its authors have no reason to keep. A
    capture that does not say which build it came from cannot tell you whether
    it has expired.
    """
    from . import uiauto

    version = uiauto.package_version(adb, serial, package) or {}
    return {"package": package, **version}


def capture_screen(adb: AdbClientProtocol, serial: str, name: str, *,
                   package: str | None = None, root: Path | None = None,
                   now: str | None = None) -> dict:
    """Store the current screen as a set of files, and record what it is.

    Raises if the hierarchy cannot be read. Everything else is best-effort: a
    missing `dumpsys media_session` is a screen with no playback, not a failed
    capture.
    """
    from . import uiauto

    root = root or CAPTURES_ROOT
    fingerprint = device_fingerprint(adb, serial)
    directory = root / device_slug(fingerprint)
    directory.mkdir(parents=True, exist_ok=True)
    slug = _slug(name)

    # The cheap reads first, deliberately. A capture is not a snapshot: the
    # hierarchy dump takes seconds -- 12.6s on a playing watch page -- and the
    # device keeps moving underneath. Taken in the other order, the first
    # attempt at a playing watch page stored a hierarchy of a playing video
    # beside a `media_session` that said STOPPED, because the video (19s long)
    # ended while the dump was running. The dumpsys reads cost ~120ms each, so
    # taking them together first makes them mutually consistent and leaves the
    # skew where it is unavoidable: between them and the hierarchy.
    started = time.monotonic()
    stored = []
    for kind, command in READS.items():
        try:
            text = adb.shell(serial, command)
        except Exception:
            continue
        if not (text or "").strip():
            continue
        (directory / f"{slug}.{kind}.txt").write_text(text, encoding="utf-8")
        stored.append(kind)

    # A generous timeout, unlike a measured run's. `dump_timeout` there is 6s
    # because the dump sits inside an action the runner will kill, and being cut
    # off beats losing the step. A capture is not inside anything: it competes
    # with nothing and a truncated one is worthless. Found the hard way -- the
    # first attempt at a playing watch page failed at exactly 6.0s, which is the
    # screen where a dump is slowest and the capture matters most.
    xml = uiauto.dump_hierarchy(adb, serial, timeout=DUMP_TIMEOUT)
    nodes = uiauto.parse_hierarchy(xml)
    if not nodes:
        raise ValueError(f"the hierarchy for {name!r} parsed to no nodes; not storing it")
    (directory / f"{slug}.hierarchy.xml").write_text(xml, encoding="utf-8")
    stored.append("hierarchy")
    elapsed = round(time.monotonic() - started, 1)

    entry = {
        "name": slug,
        "captured_at": now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "device": fingerprint,
        "reads": stored,
        "node_count": len(nodes),
        # How long the reads spanned. Recorded because a capture is a window,
        # not an instant, and a reader comparing two of its files needs to know
        # how far apart they were taken.
        "capture_seconds": elapsed,
        # `focus` is recorded so a capture can be checked against what it claims
        # to be. A capture of the wrong screen is worse than none: it looks like
        # evidence. The first attempt at `shorts` stored the launcher, because a
        # guessed coordinate missed the tab, and this field is what caught it.
        "focus": None,
        "app": None,
    }
    focus = uiauto.current_focus(adb, serial)
    if focus:
        entry["focus"] = {"package": focus[0], "activity": focus[1]}
    if package:
        entry["app"] = app_fingerprint(adb, serial, package)

    _write_manifest_entry(directory, entry)
    return entry


def _write_manifest_entry(directory: Path, entry: dict) -> None:
    """One manifest per device directory, keyed by capture name.

    Re-capturing a screen replaces its entry rather than appending: the point
    is what the device says *now*, and two answers for one name is the
    ambiguity a capture exists to remove.
    """
    path = directory / MANIFEST
    manifest: dict = {"captures": {}}
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    manifest.setdefault("captures", {})[entry["name"]] = entry
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def list_captures(root: Path | None = None) -> list[dict]:
    """Every stored capture, newest device directory first."""
    root = root or CAPTURES_ROOT
    if not root.exists():
        return []
    found = []
    for directory in sorted(root.iterdir()):
        path = directory / MANIFEST
        if not path.is_dir() and path.exists():
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for entry in manifest.get("captures", {}).values():
                found.append({"device_dir": directory.name, **entry})
    return found
