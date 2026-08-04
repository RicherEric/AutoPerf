"""Make a decayed selector happen on purpose.

The single most important thing this framework does is invisible when
everything works: a run that resolves every element by identity looks exactly
like a run that resolved them by hardcoded coordinate, unless you go looking
for the `selector_fallback` events. Demonstrating that difference used to mean
editing `scenarios/selectors.py` live and restarting the worker.

`blind_targets` does the same thing as a parameter. It takes a built scenario
and returns a copy in which the named targets have no selectors left, so
`uiauto.resolve` has nothing to match and falls through to the coordinate --
which is precisely what a YouTube update does to a stale entry in the selector
table. The run still completes and still produces a full set of real metrics;
what changes is that it now carries the evidence.

This is a demonstration and diagnosis aid, not a test double: it drives a real
device through a real scenario. Nothing here is reachable unless a caller
passes target names, and the run records which ones it blinded, so a blinded
run can never be mistaken for a genuine finding.
"""

from __future__ import annotations

from dataclasses import replace

from .scenarios import selectors as selector_table


def known_target_names() -> list[str]:
    """Every target that can be blinded, for a UI that offers a choice."""
    return sorted(target.name for target in selector_table.ALL_TARGETS)


def blind_targets(steps: list, names: list[str] | None) -> list:
    """Copy `steps`, stripping the selectors from every target in `names`.

    Unknown names raise rather than being ignored. A typo that silently
    blinded nothing would produce a demo that shows a clean run while claiming
    to show a broken one -- the same confident-green failure this whole layer
    is built to prevent.
    """
    if not names:
        return steps

    wanted = set(names)
    unknown = wanted - set(known_target_names())
    if unknown:
        raise ValueError(f"unknown target(s): {', '.join(sorted(unknown))}")

    blinded = []
    for step in steps:
        target = step.kwargs.get("target")
        if target is not None and target.name in wanted:
            # `fallback` is deliberately kept: a target with neither selectors
            # nor a coordinate reports MISSING, which is a different story
            # (the element could not be found at all) from the one being
            # demonstrated (it was found, but only by coordinate).
            step = replace(step, kwargs={**step.kwargs, "target": replace(target, selectors=())})
        blinded.append(step)
    return blinded


# --- The demo itself -------------------------------------------------------
#
# One definition, two front ends. `scripts/demo.py` drives it from a terminal
# and the dashboard's Demo page drives it from a browser; before this lived
# here they were two copies of the same timeline, and a copy of a talk track
# is a copy that will disagree with the one being presented from.
#
# Data only -- no timing, no HTTP, no presentation. What each front end does
# with it is its own business.

# Run before the demo. Everything the live three minutes needs to *show* is
# produced here, because it takes longer than the demo does:
#   1-3  one run each, which the worker turns into that scenario's baseline
#        (dashboard.tasks sets one when a device+scenario has none), so the
#        live runs have something to be compared against.
#   4    repeat data -- four iterations, because three cannot show spread.
#   5-6  Q&A material: the same scenario twice, the second with its selector
#        stripped so it falls back to a coordinate. Side by side they are the
#        evidence for "what happens when the selector table expires".
DEMO_PREWARM = (
    {"label": "建立 cold_start 的基準線",
     "kind": "run", "scenario": "cold_start", "duration": 12},
    {"label": "建立 play_golden 的基準線",
     "kind": "run", "scenario": "play_golden", "duration": 30},
    {"label": "建立 play_baby_groot_dancing 的基準線",
     "kind": "run", "scenario": "play_baby_groot_dancing", "duration": 30},
    {"label": "壓力測試資料:play_golden 重複 4 輪",
     "kind": "campaign", "scenario": "play_golden", "duration": 25, "iterations": 4},
    {"label": "Q&A 備料:selector 正常的對照組",
     "kind": "run", "scenario": "home_feed_tap_video", "duration": 25},
    {"label": "Q&A 備料:selector 失效(掉回座標)",
     "kind": "run", "scenario": "home_feed_tap_video", "duration": 25,
     "blind": ("home_feed_video",)},
)

# The demo run itself: one fixed sequence, started on every selected device
# at once. Roughly three minutes of wall-clock, and the same three minutes on
# each device because devices run in parallel -- adding a second phone costs
# no extra time, which is the point being demonstrated.
#
# What each one is here to show, in order:
#   cold_start          it really drives a phone: the screen lights up by itself
#   play_golden         a deep link opens one fixed video -- reproducible, 0 dumps
#   play_baby_groot…    a second fixed video, so two runs can be compared at all
#   home_feed_tap_video the only one that locates an element by identity, so the
#                       selector machinery is visible (and its verdict is honest
#                       if it fails -- "unverified" is the interesting outcome)
DEMO_INTEGRATION = (
    {"scenario": "cold_start", "duration": 10,
     "why": "手機自己亮起來,YouTube 到前景 —— 證明它真的在操作一台裝置"},
    {"scenario": "play_golden", "duration": 40,
     "why": "deep link 開固定影片,0 次畫面 dump —— 每次都是同一支,才比得下去"},
    {"scenario": "play_baby_groot_dancing", "duration": 40,
     "why": "第二支固定影片 —— 兩支的指標可以互相對照"},
    {"scenario": "home_feed_tap_video", "duration": 25,
     "why": "唯一會 dump 畫面、靠 selector 找元素的一個 —— 定位機制看得到"},
)

# Sampling time plus what each run costs around it (launch, verify, force-stop).
DEMO_RUN_OVERHEAD_SECONDS = 20
DEMO_INTEGRATION_SECONDS = sum(
    step["duration"] + DEMO_RUN_OVERHEAD_SECONDS for step in DEMO_INTEGRATION)

