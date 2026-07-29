"""Check every UI selector against one real device before measuring anything.

The selectors in `scenarios/selectors.py` are guesses until someone confirms
them against the app: resource-ids are internal to each build and published
nowhere, and accessibility labels are translated. Until now the only way to
discover a wrong one was to run a full measured test and read its events
afterwards -- by which point a suite has already spent an hour producing
numbers that describe screens it never reached.

Preflight answers that question first, on one device, in one pass. It is not
a dry run in the usual sense: the taps really happen, because most targets
only exist once the preceding step has navigated to their screen. What it
skips is the measurement. What it adds is a record, per target, of *how* the
target was found -- by selector, or by falling through to the coordinate that
selectors were meant to replace -- plus, when a selector misses, a listing of
what was actually on screen at that moment, so the correct selector can be
read straight off the report rather than guessed at again.

Nothing here touches Storage. A preflight produces no run, no metrics and no
history; it is a question about the device and the selector table, not a
measurement of either.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import uiauto
from .adapters import Adapter, VerificationError
from .adb import AdbClientProtocol
from .scenarios import youtube as youtube_scenarios

# How each target resolved, worst last -- the ordering is used to decide a
# scenario's overall outcome.
MATCHED = "selector"        # found by a selector: this is what "working" means
FALLBACK = "coordinates"    # worked, but only via the hardcoded coordinate
MISSING = "missing"         # not found at all, and no coordinate to fall back to
ERROR = "error"             # the check itself could not be performed

DEGRADED = (FALLBACK, MISSING, ERROR)

# Steps that assert rather than navigate: graded by their own outcome instead
# of by how a target resolved. Listed here rather than inline so adding an
# assertion to the adapter does not silently leave preflight ungraded -- the
# test below pins this list against the actions the scenarios actually use.
VERIFICATION_ACTIONS = ("verify_foreground", "verify_playing", "verify_element_state")

# How many on-screen elements to capture when a target degrades. Enough to
# find the intended control in, small enough to read.
OBSERVED_LIMIT = 25


@dataclass(frozen=True, slots=True)
class TargetCheck:
    scenario: str
    at: float
    target: str
    status: str
    strategy: str | None = None
    selector_index: int | None = None
    detail: str = ""
    observed: list[dict] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    scenario: str
    at: float
    action: str
    result: str          # "passed" | "failed" | "unknown"
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    scenario: str
    targets: list[TargetCheck]
    verifications: list[VerificationCheck]

    @property
    def ok(self) -> bool:
        return (not any(check.status in DEGRADED for check in self.targets)
                and not any(check.result == "failed" for check in self.verifications))


def covering_scenarios(names: list[str] | None = None) -> list[str]:
    """The fewest scenarios that still exercise every distinct target.

    Most presets share `_enter_video_steps`, so running all of them would
    check `search_icon` a dozen times while adding nothing. A greedy set
    cover keeps preflight to a few minutes without dropping a single target
    from the check -- which is the whole point of it.
    """
    remaining: dict[str, set[str]] = {}
    for name in names or youtube_scenarios.list_scenarios():
        targets = {
            step.kwargs["target"].name
            for step in youtube_scenarios.build(name, (1080, 2340))
            if step.action == "tap_element" and step.kwargs.get("target") is not None
        }
        if targets:
            remaining[name] = targets

    uncovered: set[str] = set().union(*remaining.values()) if remaining else set()
    chosen: list[str] = []
    while uncovered:
        # Ties broken by name so the selection is deterministic -- a preflight
        # that checked a different subset each time would be useless as a gate.
        best = max(sorted(remaining), key=lambda n: len(remaining[n] & uncovered))
        gain = remaining[best] & uncovered
        if not gain:
            break
        chosen.append(best)
        uncovered -= gain
    return chosen


def _observe(adb: AdbClientProtocol, serial: str) -> list[dict]:
    """What is on screen right now, for a report the reader can act on."""
    try:
        nodes = uiauto.parse_hierarchy(uiauto.dump_hierarchy(adb, serial))
    except Exception:
        return []
    return uiauto.describe_clickables(nodes, limit=OBSERVED_LIMIT)


def check_scenario(adb: AdbClientProtocol, adapter: Adapter, serial: str, scenario: str, *,
                   screen: tuple[int, int] | None = None, sleep=None,
                   on_check=None) -> ScenarioReport:
    """Walk one scenario on the device, recording how each target resolved.

    The steps are performed for real, in their scripted order and at their
    scripted times: skipping the taps would leave every later screen
    unreached, and every target on it would be reported missing for a reason
    that has nothing to do with its selector.

    `sleep` defaults to None rather than to `time.sleep` itself, so the lookup
    happens per call. A default of `sleep=time.sleep` binds the function
    object at import, which quietly makes the seam unpatchable: a caller that
    patches `time.sleep` still gets the original, and the paced timeline runs
    at wall-clock speed. That is what made the preflight CLI tests take six
    seconds each while appearing to have stubbed the wait out.
    """
    sleep = time.sleep if sleep is None else sleep
    screen = screen or adapter.screen_size(adb, serial)
    steps = sorted(youtube_scenarios.build(scenario, screen), key=lambda s: s.at)
    targets: list[TargetCheck] = []
    verifications: list[VerificationCheck] = []
    started = time.monotonic()

    for step in steps:
        delay = step.at - (time.monotonic() - started)
        if delay > 0:
            sleep(delay)

        if step.action == "tap_element":
            check = _check_target(adb, adapter, serial, scenario, step, screen)
            targets.append(check)
            if on_check is not None:
                on_check(check)
            continue

        if step.action in VERIFICATION_ACTIONS:
            verifications.append(_check_verification(adapter, adb, serial, scenario, step))
            continue

        # Navigation steps (launch, swipe, key events) are performed but not
        # graded: none of them depend on a selector, so there is nothing here
        # for a preflight to find wrong.
        try:
            getattr(adapter, step.action)(adb, serial, **step.kwargs)
        except Exception:
            pass

    return ScenarioReport(scenario, targets, verifications)


def _check_target(adb, adapter, serial, scenario, step, screen) -> TargetCheck:
    target = step.kwargs["target"]
    try:
        nodes = uiauto.parse_hierarchy(uiauto.dump_hierarchy(adb, serial))
        dump_failed = False
    except Exception as exc:
        nodes, dump_failed = [], str(exc)

    resolution = uiauto.resolve(target, nodes, screen)

    if resolution is None:
        return TargetCheck(scenario, step.at, target.name, MISSING,
                           detail="no selector matched and the target has no coordinate fallback",
                           observed=uiauto.describe_clickables(nodes, limit=OBSERVED_LIMIT))

    # Perform the tap regardless of how it resolved, so the scenario keeps
    # advancing and the targets after this one are still reachable.
    try:
        adapter.tap(adb, serial, *resolution.point)
    except Exception as exc:
        return TargetCheck(scenario, step.at, target.name, ERROR, detail=str(exc))

    if resolution.strategy == "coordinates":
        detail = (f"uiautomator dump failed ({dump_failed}); could not evaluate selectors"
                  if dump_failed else "no selector matched; fell through to the coordinate")
        return TargetCheck(scenario, step.at, target.name, FALLBACK, strategy="coordinates",
                           detail=detail,
                           observed=uiauto.describe_clickables(nodes, limit=OBSERVED_LIMIT))

    return TargetCheck(scenario, step.at, target.name, MATCHED,
                       strategy=resolution.strategy, selector_index=resolution.selector_index)


def _check_verification(adapter, adb, serial, scenario, step) -> VerificationCheck:
    try:
        outcome = getattr(adapter, step.action)(adb, serial, **step.kwargs)
    except VerificationError as exc:
        return VerificationCheck(scenario, step.at, step.action, "failed", str(exc))
    except Exception as exc:
        return VerificationCheck(scenario, step.at, step.action, "unknown", str(exc))
    verified = outcome.get("verified") if isinstance(outcome, dict) else None
    if verified is None:
        return VerificationCheck(scenario, step.at, step.action, "unknown",
                                 "device did not report a state either way")
    return VerificationCheck(scenario, step.at, step.action, "passed")


def summarise(reports: list[ScenarioReport]) -> dict:
    """Aggregate per target, because the fix is per target.

    A stale selector shows up once per scenario that uses it, but it is
    corrected in exactly one place -- `scenarios/selectors.py`. Reporting it
    once, with the scenarios it affected and what was on screen, is what makes
    the output a work list rather than a log.
    """
    by_target: dict[str, dict] = {}
    for report in reports:
        for check in report.targets:
            entry = by_target.setdefault(check.target, {
                "target": check.target, "checked": 0, "matched": 0,
                "statuses": set(), "scenarios": set(), "strategies": set(),
                "detail": "", "observed": [],
            })
            entry["checked"] += 1
            entry["statuses"].add(check.status)
            entry["scenarios"].add(check.scenario)
            if check.status == MATCHED:
                entry["matched"] += 1
                entry["strategies"].add(check.strategy)
            else:
                if not entry["detail"]:
                    entry["detail"] = check.detail
                if not entry["observed"]:
                    entry["observed"] = check.observed

    needs_attention, healthy = [], []
    for entry in sorted(by_target.values(), key=lambda e: e["target"]):
        record = {
            "target": entry["target"],
            "checked": entry["checked"],
            "matched": entry["matched"],
            "scenarios": sorted(entry["scenarios"]),
            "strategies": sorted(s for s in entry["strategies"] if s),
        }
        if entry["matched"] == entry["checked"]:
            healthy.append(record)
        else:
            needs_attention.append({
                **record,
                "status": sorted(entry["statuses"] - {MATCHED}),
                "detail": entry["detail"],
                "current_selectors": _describe_selectors(entry["target"]),
                "observed_on_screen": entry["observed"],
            })

    failed_verifications = [
        {"scenario": v.scenario, "action": v.action, "at": v.at, "detail": v.detail}
        for report in reports for v in report.verifications if v.result == "failed"
    ]
    return {
        "scenarios_checked": [r.scenario for r in reports],
        "targets_checked": len(by_target),
        "targets_ok": len(healthy),
        "targets_needing_attention": len(needs_attention),
        "needs_attention": needs_attention,
        "healthy": healthy,
        "failed_verifications": failed_verifications,
        "ok": not needs_attention and not failed_verifications,
    }


def _describe_selectors(target_name: str) -> list[dict]:
    """What the selector table currently says, so the report is self-contained."""
    from .scenarios import selectors as selector_table

    target = next((t for t in selector_table.ALL_TARGETS if t.name == target_name), None)
    if target is None:
        return []
    described = []
    for selector in target.selectors:
        described.append({
            key: value for key, value in (
                ("resource_id", selector.resource_id),
                ("content_desc", selector.content_desc),
                ("text", selector.text),
                ("class_name", selector.class_name),
                ("index", selector.index),
            ) if value is not None
        })
    return described


def _packages_of(scenario: str) -> set[str]:
    return {
        step.kwargs["package"]
        for step in youtube_scenarios.build(scenario, (1080, 2340))
        if step.action == "launch_app" and step.kwargs.get("package")
    }


def _reset(adb: AdbClientProtocol, adapter: Adapter, serial: str, scenario: str, sleep) -> None:
    """Force-stop the scenario's apps so the next one starts from a clean state.

    Without this, state leaks between scenarios and produces findings that
    have nothing to do with selectors. Observed on a Galaxy A55: after
    `home_feed_tap_video` opened a video, the following scenario's `launch_app`
    merely returned the already-running app to the foreground -- still on the
    watch screen -- so its bottom-navigation target was legitimately absent and
    was reported as a decayed selector.

    Real runs do not have this problem: each is a separate TestRunner.run()
    that force-stops the app when it finishes. Preflight has to do the same
    thing itself.
    """
    for package in _packages_of(scenario):
        try:
            adapter.stop_app(adb, serial, package)
        except Exception:
            pass
    sleep(1.0)


def run_preflight(adb: AdbClientProtocol, adapter: Adapter, serial: str, *,
                  scenarios: list[str] | None = None, sleep=None,
                  on_scenario=None, on_check=None) -> dict:
    """Check one device, then stop. Returns the summary; writes nothing.

    See `check_scenario` for why `sleep` defaults to None.
    """
    sleep = time.sleep if sleep is None else sleep
    names = scenarios or covering_scenarios()
    screen = adapter.screen_size(adb, serial)
    reports = []
    for name in names:
        if on_scenario is not None:
            on_scenario(name)
        _reset(adb, adapter, serial, name, sleep)
        reports.append(check_scenario(adb, adapter, serial, name,
                                      screen=screen, sleep=sleep, on_check=on_check))
    summary = summarise(reports)
    summary["serial"] = serial
    summary["screen"] = list(screen)
    return summary
