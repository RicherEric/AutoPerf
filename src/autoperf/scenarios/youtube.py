from __future__ import annotations

from ..adapters import APP_SWITCH, BACK, HOME, ScenarioStep
from . import coords

PACKAGE = "com.google.android.youtube"


def _launch(at: float = 0.0) -> ScenarioStep:
    return ScenarioStep(at, "launch_app", {"package": PACKAGE})


def _enter_video_steps(screen, start_at: float = 0.0) -> list[ScenarioStep]:
    """Launch, search, and tap into a result -- lands on a playing video by ~start_at+8.0."""
    return [
        _launch(start_at + 0.0),
        ScenarioStep(start_at + 3.0, "tap", coords.rel_tap(screen, 0.92, 0.06)),   # search icon
        ScenarioStep(start_at + 4.5, "tap", coords.rel_tap(screen, 0.5, 0.08)),    # search bar
        ScenarioStep(start_at + 6.0, "tap", coords.rel_tap(screen, 0.5, 0.2)),     # first suggestion
        ScenarioStep(start_at + 8.0, "tap", coords.rel_tap(screen, 0.5, 0.35)),    # result thumbnail
    ]


def cold_start(screen) -> list[ScenarioStep]:
    return [_launch(0.0)]


def cold_start_and_stop(screen) -> list[ScenarioStep]:
    return [_launch(0.0), ScenarioStep(8.0, "stop_app", {"package": PACKAGE})]


def search_and_play(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0)


def home_feed_scroll(screen) -> list[ScenarioStep]:
    steps = [_launch(0.0)]
    for at in (3.0, 6.0, 9.0):
        steps.append(ScenarioStep(at, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)))
    return steps


def home_feed_tap_video(screen) -> list[ScenarioStep]:
    return [
        _launch(0.0),
        ScenarioStep(3.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(5.0, "tap", coords.rel_tap(screen, 0.5, 0.45)),
    ]


def shorts_browsing(screen) -> list[ScenarioStep]:
    steps = [
        _launch(0.0),
        ScenarioStep(3.0, "tap", coords.rel_tap(screen, 0.6, 0.95)),  # Shorts tab
    ]
    for at in (5.0, 7.0, 9.0, 11.0, 13.0):
        steps.append(ScenarioStep(at, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.2, duration_ms=250)))
    return steps


def shorts_like_and_next(screen) -> list[ScenarioStep]:
    return [
        _launch(0.0),
        ScenarioStep(3.0, "tap", coords.rel_tap(screen, 0.6, 0.95)),
        ScenarioStep(5.0, "tap", coords.rel_tap(screen, 0.9, 0.55)),  # like
        ScenarioStep(6.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.2)),
        ScenarioStep(8.0, "tap", coords.rel_tap(screen, 0.9, 0.55)),
    ]


def quality_switch_manual(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(9.0, "tap", coords.rel_tap(screen, 0.95, 0.4)),   # overflow menu
        ScenarioStep(10.0, "tap", coords.rel_tap(screen, 0.5, 0.55)),  # Quality row
        ScenarioStep(11.0, "tap", coords.rel_tap(screen, 0.5, 0.4)),   # a resolution entry
    ]


def like_video(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(9.0, "tap", coords.rel_tap(screen, 0.15, 0.62)),
    ]


def comment_scroll(screen) -> list[ScenarioStep]:
    steps = _enter_video_steps(screen, 0.0) + [
        ScenarioStep(9.0, "tap", coords.rel_tap(screen, 0.5, 0.68)),  # comments row
    ]
    for at in (10.5, 12.5, 14.5):
        steps.append(ScenarioStep(at, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.35)))
    steps.append(ScenarioStep(16.5, "key_event", {"keycode": BACK}))
    return steps


def fullscreen_toggle_cycle(screen) -> list[ScenarioStep]:
    steps = _enter_video_steps(screen, 0.0)
    for i, at in enumerate((9.0, 12.0, 15.0, 18.0)):
        fy = 0.58 if i % 2 == 0 else 0.9
        steps.append(ScenarioStep(at, "tap", coords.rel_tap(screen, 0.93, fy)))
    return steps


def seek_scrub_forward(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(9.0, "tap", coords.rel_tap(screen, 0.5, 0.5)),  # reveal player controls
        ScenarioStep(10.0, "swipe", coords.rel_swipe(screen, 0.3, 0.94, 0.7, 0.94)),
        ScenarioStep(13.0, "swipe", coords.rel_swipe(screen, 0.5, 0.94, 0.65, 0.94)),
    ]


def seek_long_press_skip(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(9.0, "swipe", coords.long_press(screen, 0.8, 0.5, duration_ms=900)),
        ScenarioStep(12.0, "swipe", coords.long_press(screen, 0.8, 0.5, duration_ms=900)),
    ]


def background_foreground_resume(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(10.0, "key_event", {"keycode": HOME}),
        ScenarioStep(15.0, "launch_app", {"package": PACKAGE}),
    ]


def app_switch_cycle(screen) -> list[ScenarioStep]:
    steps = _enter_video_steps(screen, 0.0)
    for at in (10.0, 13.0, 16.0, 19.0):
        steps.append(ScenarioStep(at, "key_event", {"keycode": APP_SWITCH}))
    return steps


def pip_minimize(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(9.0, "tap", coords.rel_tap(screen, 0.06, 0.42)),  # PiP caret
        ScenarioStep(11.0, "swipe", coords.rel_swipe(screen, 0.8, 0.2, 0.2, 0.7)),
    ]


def multi_video_session(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(12.0, "key_event", {"keycode": BACK}),
        ScenarioStep(13.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(15.0, "tap", coords.rel_tap(screen, 0.5, 0.45)),   # video B
        ScenarioStep(21.0, "key_event", {"keycode": BACK}),
        ScenarioStep(23.0, "tap", coords.rel_tap(screen, 0.5, 0.6)),    # video C
    ]


def subscriptions_feed_browse(screen) -> list[ScenarioStep]:
    return [
        _launch(0.0),
        ScenarioStep(3.0, "tap", coords.rel_tap(screen, 0.4, 0.95)),  # Subscriptions tab
        ScenarioStep(5.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(7.5, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(9.5, "tap", coords.rel_tap(screen, 0.5, 0.45)),
    ]


def library_and_downloads_browse(screen) -> list[ScenarioStep]:
    return [
        _launch(0.0),
        ScenarioStep(3.0, "tap", coords.rel_tap(screen, 0.8, 0.95)),  # Library tab
        ScenarioStep(5.0, "tap", coords.rel_tap(screen, 0.5, 0.3)),   # Downloads row
        ScenarioStep(8.0, "key_event", {"keycode": BACK}),
    ]


REGISTRY = {
    "cold_start": cold_start,
    "cold_start_and_stop": cold_start_and_stop,
    "search_and_play": search_and_play,
    "home_feed_scroll": home_feed_scroll,
    "home_feed_tap_video": home_feed_tap_video,
    "shorts_browsing": shorts_browsing,
    "shorts_like_and_next": shorts_like_and_next,
    "quality_switch_manual": quality_switch_manual,
    "like_video": like_video,
    "comment_scroll": comment_scroll,
    "fullscreen_toggle_cycle": fullscreen_toggle_cycle,
    "seek_scrub_forward": seek_scrub_forward,
    "seek_long_press_skip": seek_long_press_skip,
    "background_foreground_resume": background_foreground_resume,
    "app_switch_cycle": app_switch_cycle,
    "pip_minimize": pip_minimize,
    "multi_video_session": multi_video_session,
    "subscriptions_feed_browse": subscriptions_feed_browse,
    "library_and_downloads_browse": library_and_downloads_browse,
}


def list_scenarios() -> list[str]:
    return sorted(REGISTRY)


def build(name: str, screen: tuple[int, int]) -> list[ScenarioStep]:
    try:
        builder = REGISTRY[name]
    except KeyError:
        raise ValueError(f"Unknown YouTube scenario: {name!r}") from None
    return builder(screen)
