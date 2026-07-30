#!/usr/bin/env python
"""Run the tests, all of them or one group.

Two things this fixes. The suites needed two different commands -- `pytest` for
the core and `manage.py test` for the webapp -- so "run everything" was a thing
you had to remember rather than a thing you could do. And the suite was one flat
list of 431 tests, which meant the question a maintainer actually asks --
"would any of these catch a YouTube update?" -- had no way to be answered.

The groups below are by **what makes a test fail**, not by which file it is in.
That is the only division that helps: it tells you which group to look at when
something breaks, and which group to add to when you write something new.

    python scripts/run-tests.py                # everything, core then webapp
    python scripts/run-tests.py logic          # one group
    python scripts/run-tests.py parsing elements   # several
    python scripts/run-tests.py --list         # what the groups are

Uses unittest, not pytest, so it needs nothing installed that the project does
not already need.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "logic": (
        "Our own logic. Nothing here knows a device exists, so a device or app "
        "change can never break it.",
        ("test_models", "test_analyzer", "test_storage", "test_scenarios_coords",
         "test_scenarios_youtube", "test_screen_stream", "test_suite_groups"),
    ),
    "parsing": (
        "Reading what a device says. Every hand-written fixture of device output "
        "lives here, which makes this the group that goes stale when a device or "
        "Android version differs from what was assumed.",
        ("test_adb", "test_uiauto", "test_collectors_errors"),
    ),
    "commands": (
        "What we send to a device: command strings, argument validation, adapter "
        "selection. Breaks when a command's shape changes.",
        ("test_adapters",),
    ),
    "elements": (
        "Deciding what to touch and proving it worked: selector resolution, the "
        "verify_* actions, TV focus walking.",
        ("test_adapters_elements",),
    ),
    "flow": (
        "Whole flows against a fake device: runner, campaigns, preflight, CLI.",
        ("test_core", "test_runner", "test_campaigns", "test_preflight", "test_cli",
         "test_workers"),
    ),
}

WEBAPP = ("dashboard", "livescreen")


def core(names: tuple[str, ...], verbosity: int) -> bool:
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for name in names:
        suite.addTests(loader.loadTestsFromName(f"tests.{name}"))
    return unittest.TextTestRunner(verbosity=verbosity).run(suite).wasSuccessful()


def webapp(verbosity: int) -> bool:
    # A separate process because Django has to configure settings first, and
    # doing that inside this one would leak into the core run.
    return subprocess.run(
        [sys.executable, str(ROOT / "webapp" / "manage.py"), "test", *WEBAPP,
         f"--verbosity={verbosity}"],
        cwd=ROOT,
    ).returncode == 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    verbosity = 2 if "-v" in argv or "--verbose" in argv else 1

    if "--list" in argv:
        width = max(len(name) for name in (*GROUPS, "webapp"))
        for name, (why, modules) in GROUPS.items():
            print(f"{name:<{width}}  {len(modules)} modules")
            print(f"{'':<{width}}  {why}")
        print(f"{'webapp':<{width}}  Django API, Celery tasks, live-screen streaming")
        return 0

    unknown = [a for a in args if a not in GROUPS and a != "webapp"]
    if unknown:
        print(f"unknown group(s): {', '.join(unknown)}\n"
              f"known: {', '.join([*GROUPS, 'webapp'])}", file=sys.stderr)
        return 2

    chosen = args or [*GROUPS, "webapp"]
    ok = True
    core_names = tuple(m for name in chosen if name in GROUPS for m in GROUPS[name][1])
    if core_names:
        ok = core(core_names, verbosity) and ok
    if "webapp" in chosen:
        ok = webapp(verbosity) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
