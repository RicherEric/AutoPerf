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
