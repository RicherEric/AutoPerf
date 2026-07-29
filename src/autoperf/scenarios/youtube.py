from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..adapters import APP_SWITCH, BACK, HOME, ScenarioStep
from . import coords, selectors

PACKAGE = "com.google.android.youtube"
SETTINGS_PACKAGE = "com.android.settings"

# Depth tiers, shallow to deep -- the same idea as smoke tests vs. a full
# daily regression suite: SMOKE only checks "does the app even launch and
# reach basic content" (fast, run often); FUNCTIONAL covers common everyday
# interactions; REGRESSION covers deeper/edge-case flows worth running on a
# slower cadence (e.g. nightly) precisely because they take longer and touch
# less-common paths.
TIER_SMOKE = "smoke"
TIER_FUNCTIONAL = "functional"
TIER_REGRESSION = "regression"
TIERS = (TIER_SMOKE, TIER_FUNCTIONAL, TIER_REGRESSION)


@dataclass(frozen=True, slots=True)
class ScenarioPreset:
    name: str
    description: str
    tier: str
    build: Callable[[tuple[int, int]], list[ScenarioStep]]


def _launch(at: float = 0.0) -> ScenarioStep:
    return ScenarioStep(at, "launch_app", {"package": PACKAGE})


def _launch_verified(at: float = 0.0, package: str = PACKAGE, settle: float = 2.5) -> list[ScenarioStep]:
    """Launch, then assert the app really came to the front.

    `am start` / `monkey` return success once the intent is *dispatched*, not
    once the app is usable, so a launch that crashes on startup or is
    swallowed by a system dialog looks identical to a good one. Every step
    after it then operates on whatever is actually on screen, and the run
    still finishes green. The check costs one `dumpsys window` read.
    """
    return [
        ScenarioStep(at, "launch_app", {"package": package}),
        ScenarioStep(at + settle, "verify_foreground", {"package": package}),
    ]


@dataclass(frozen=True, slots=True)
class NamedVideo:
    key: str
    video_id: str
    title: str  # human-readable, goes into the preset's description


# Deep-links straight to a specific, known video (via launch_app's `data` /
# Android VIEW-intent support) instead of the blind search-and-tap taps
# _enter_video_steps uses -- unlike those, replaying the *same* video every
# run gives reproducible length/resolution/content, which matters for
# baseline-vs-candidate performance comparisons. Add another NamedVideo here
# for more of the same theme; each becomes its own `play_<key>` preset below.
NAMED_VIDEOS = (
    NamedVideo("golden", "TlFrIH6GQhk", "HUNTR/X《Golden》(獵魔女團主題曲)"),
    NamedVideo("baby_groot_dancing", "DfNSBeFliIg", "《星際異攻隊2》Baby Groot 開場跳舞片段"),
    NamedVideo("suis_moi", "b0IaM4_I_Nk", "《小王子》主題曲 Suis-moi"),
    # The classic rickroll -- deliberately included, not a mistake. Note the
    # `&list=RDdQw4w9WgXcQ` (auto-play radio queue) from the original URL is
    # dropped: only the video ID is used, same as every other NamedVideo,
    # so the video's own end doesn't roll into an unpredictable playlist --
    # that would defeat the fixed-content reproducibility this whole
    # mechanism exists for.
    NamedVideo("rickroll", "dQw4w9WgXcQ", "Rick Astley《Never Gonna Give You Up》"),
)


def _play_named_video(video: NamedVideo) -> Callable[[tuple[int, int]], list[ScenarioStep]]:
    def build_fn(screen: tuple[int, int]) -> list[ScenarioStep]:
        url = f"https://www.youtube.com/watch?v={video.video_id}"
        return [
            ScenarioStep(0.0, "launch_app", {"package": PACKAGE, "data": url}),
            ScenarioStep(3.0, "verify_foreground", {"package": PACKAGE}),
            # These presets exist to give baseline comparisons fixed,
            # reproducible content -- a deep link that silently failed to
            # play would quietly undermine exactly that.
            ScenarioStep(8.0, "verify_playing", {"package": PACKAGE}),
        ]
    return build_fn


def _enter_video_steps(screen, start_at: float = 0.0) -> list[ScenarioStep]:
    """Launch, search, and tap into a result -- lands on a playing video by ~start_at+8.0."""
    return [
        *_launch_verified(start_at + 0.0),
        ScenarioStep(start_at + 3.0, "tap_element", {"target": selectors.SEARCH_ICON}),
        ScenarioStep(start_at + 4.5, "tap_element", {"target": selectors.SEARCH_BAR}),
        ScenarioStep(start_at + 6.0, "tap_element", {"target": selectors.FIRST_SUGGESTION}),
        ScenarioStep(start_at + 8.0, "tap_element", {"target": selectors.RESULT_THUMBNAIL}),
        # Proves the flow actually reached playback. A foreground check
        # would pass even if all four taps above hit empty space.
        ScenarioStep(start_at + 11.0, "verify_playing", {"package": PACKAGE}),
    ]


def cold_start(screen) -> list[ScenarioStep]:
    return _launch_verified(0.0)


def device_settings_scroll(screen) -> list[ScenarioStep]:
    """Visible smoke flow supported by both phones and Android TV."""
    return [
        *_launch_verified(0.0, SETTINGS_PACKAGE),
        ScenarioStep(3.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(6.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(9.0, "swipe", coords.rel_swipe(screen, 0.5, 0.3, 0.5, 0.8)),
        ScenarioStep(12.0, "key_event", {"keycode": HOME}),
    ]


def cold_start_and_stop(screen) -> list[ScenarioStep]:
    return [*_launch_verified(0.0), ScenarioStep(8.0, "stop_app", {"package": PACKAGE})]


def search_and_play(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0)


def home_feed_scroll(screen) -> list[ScenarioStep]:
    steps = _launch_verified(0.0)
    for at in (3.0, 6.0, 9.0):
        steps.append(ScenarioStep(at, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)))
    return steps


def home_feed_tap_video(screen) -> list[ScenarioStep]:
    return [
        *_launch_verified(0.0),
        ScenarioStep(3.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(5.0, "tap_element", {"target": selectors.HOME_FEED_VIDEO}),
    ]


def shorts_browsing(screen) -> list[ScenarioStep]:
    steps = [
        *_launch_verified(0.0),
        ScenarioStep(3.0, "tap_element", {"target": selectors.SHORTS_TAB}),
    ]
    for at in (5.0, 7.0, 9.0, 11.0, 13.0):
        steps.append(ScenarioStep(at, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.2, duration_ms=250)))
    return steps


def shorts_like_and_next(screen) -> list[ScenarioStep]:
    return [
        *_launch_verified(0.0),
        ScenarioStep(3.0, "tap_element", {"target": selectors.SHORTS_TAB}),
        ScenarioStep(5.0, "tap_element", {"target": selectors.SHORTS_LIKE_BUTTON}),
        ScenarioStep(6.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.2)),
        ScenarioStep(8.0, "tap_element", {"target": selectors.SHORTS_LIKE_BUTTON}),
    ]


def quality_switch_manual(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(12.0, "tap_element", {"target": selectors.OVERFLOW_MENU}),
        ScenarioStep(13.0, "tap_element", {"target": selectors.QUALITY_ROW}),
        ScenarioStep(14.0, "tap_element", {"target": selectors.QUALITY_OPTION}),
    ]


def like_video(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(12.0, "tap_element", {"target": selectors.LIKE_BUTTON}),
    ]


def comment_scroll(screen) -> list[ScenarioStep]:
    steps = _enter_video_steps(screen, 0.0) + [
        ScenarioStep(12.0, "tap_element", {"target": selectors.COMMENTS_ROW}),
    ]
    for at in (13.5, 15.5, 17.5):
        steps.append(ScenarioStep(at, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.35)))
    steps.append(ScenarioStep(19.5, "key_event", {"keycode": BACK}))
    return steps


def fullscreen_toggle_cycle(screen) -> list[ScenarioStep]:
    steps = _enter_video_steps(screen, 0.0)
    # Enter and exit alternate, and they are genuinely different controls --
    # the button sits in a different place in each state, which is why the
    # original had two hardcoded y positions. Naming them separately lets
    # each carry its own accessibility label ("Enter fullscreen" vs "Exit
    # fullscreen") rather than relying on the alternation being in step.
    for index, at in enumerate((12.0, 15.0, 18.0, 21.0)):
        target = selectors.FULLSCREEN_ENTER if index % 2 == 0 else selectors.FULLSCREEN_EXIT
        steps.append(ScenarioStep(at, "tap_element", {"target": target}))
    return steps


def seek_scrub_forward(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(12.0, "tap_element", {"target": selectors.PLAYER_SURFACE}),
        ScenarioStep(13.5, "swipe", coords.rel_swipe(screen, 0.3, 0.94, 0.7, 0.94)),
        ScenarioStep(16.0, "swipe", coords.rel_swipe(screen, 0.5, 0.94, 0.65, 0.94)),
    ]


def seek_long_press_skip(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(12.5, "swipe", coords.long_press(screen, 0.8, 0.5, duration_ms=900)),
        ScenarioStep(15.5, "swipe", coords.long_press(screen, 0.8, 0.5, duration_ms=900)),
    ]


def background_foreground_resume(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(14.0, "key_event", {"keycode": HOME}),
        ScenarioStep(19.0, "launch_app", {"package": PACKAGE}),
        # The whole point of the scenario is that playback survives the
        # round trip, so resuming has to be asserted, not assumed.
        ScenarioStep(23.0, "verify_playing", {"package": PACKAGE}),
    ]


def app_switch_cycle(screen) -> list[ScenarioStep]:
    steps = _enter_video_steps(screen, 0.0)
    # Starts after _enter_video_steps' playback check at 11.0 -- switching
    # away before it would leave the assertion racing the recents screen.
    for at in (13.0, 16.0, 19.0, 22.0):
        steps.append(ScenarioStep(at, "key_event", {"keycode": APP_SWITCH}))
    return steps


def pip_minimize(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(12.0, "tap_element", {"target": selectors.PIP_CARET}),
        ScenarioStep(14.0, "swipe", coords.rel_swipe(screen, 0.8, 0.2, 0.2, 0.7)),
    ]


def multi_video_session(screen) -> list[ScenarioStep]:
    return _enter_video_steps(screen, 0.0) + [
        ScenarioStep(12.0, "key_event", {"keycode": BACK}),
        ScenarioStep(13.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(15.0, "tap_element", {"target": selectors.SECOND_VIDEO}),
        ScenarioStep(21.0, "key_event", {"keycode": BACK}),
        ScenarioStep(23.0, "tap_element", {"target": selectors.THIRD_VIDEO}),
    ]


def subscriptions_feed_browse(screen) -> list[ScenarioStep]:
    return [
        *_launch_verified(0.0),
        ScenarioStep(3.0, "tap_element", {"target": selectors.SUBSCRIPTIONS_TAB}),
        ScenarioStep(5.0, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(7.5, "swipe", coords.rel_swipe(screen, 0.5, 0.8, 0.5, 0.3)),
        ScenarioStep(9.5, "tap_element", {"target": selectors.SUBSCRIPTION_VIDEO}),
    ]


def library_and_downloads_browse(screen) -> list[ScenarioStep]:
    return [
        *_launch_verified(0.0),
        ScenarioStep(3.0, "tap_element", {"target": selectors.LIBRARY_TAB}),
        ScenarioStep(5.0, "tap_element", {"target": selectors.DOWNLOADS_ROW}),
        ScenarioStep(8.0, "key_event", {"keycode": BACK}),
    ]


REGISTRY: dict[str, ScenarioPreset] = {
    preset.name: preset
    for preset in (
        ScenarioPreset(
            "device_settings_scroll",
            "開啟系統設定、上下滑動並返回桌面；手機與 Android TV 都能看到明顯操作。",
            TIER_SMOKE,
            device_settings_scroll,
        ),
        ScenarioPreset("cold_start", "冷啟動開啟 YouTube App,最基本的存活檢查(能不能正常開啟)。", TIER_SMOKE, cold_start),
        ScenarioPreset("cold_start_and_stop", "開啟 YouTube 後強制關閉,驗證啟動與關閉流程都正常。", TIER_SMOKE, cold_start_and_stop),
        ScenarioPreset("search_and_play", "開啟搜尋、輸入關鍵字、點擊建議、播放第一個結果影片,涵蓋搜尋到播放的核心流程。", TIER_SMOKE, search_and_play),
        ScenarioPreset("home_feed_scroll", "在首頁動態消息連續往下滑動三次,測試瀏覽首頁 feed 的流暢度。", TIER_SMOKE, home_feed_scroll),
        ScenarioPreset("home_feed_tap_video", "滑動首頁一次後點擊一支影片縮圖進入播放。", TIER_FUNCTIONAL, home_feed_tap_video),
        ScenarioPreset("like_video", "進入影片播放後對影片按讚。", TIER_FUNCTIONAL, like_video),
        ScenarioPreset("shorts_browsing", "切到 Shorts 分頁,連續快速上滑瀏覽 5 支短影音。", TIER_FUNCTIONAL, shorts_browsing),
        ScenarioPreset("shorts_like_and_next", "在 Shorts 按讚後滑到下一支,再按一次讚。", TIER_FUNCTIONAL, shorts_like_and_next),
        ScenarioPreset("subscriptions_feed_browse", "切到訂閱分頁並滑動瀏覽,再點擊一支影片。", TIER_FUNCTIONAL, subscriptions_feed_browse),
        ScenarioPreset("library_and_downloads_browse", "切到媒體庫分頁,進入下載項目後返回。", TIER_FUNCTIONAL, library_and_downloads_browse),
        ScenarioPreset("comment_scroll", "進入影片播放後開啟留言區並滑動瀏覽留言,最後返回。", TIER_FUNCTIONAL, comment_scroll),
        ScenarioPreset("fullscreen_toggle_cycle", "進入影片播放後反覆切換全螢幕/退出全螢幕。", TIER_FUNCTIONAL, fullscreen_toggle_cycle),
        ScenarioPreset("quality_switch_manual", "進入影片播放後,手動開啟選單切換畫質。", TIER_REGRESSION, quality_switch_manual),
        ScenarioPreset("seek_scrub_forward", "進入影片播放後在進度條上快轉拖曳兩次。", TIER_REGRESSION, seek_scrub_forward),
        ScenarioPreset("seek_long_press_skip", "進入影片播放後用長按方式快轉跳過片段。", TIER_REGRESSION, seek_long_press_skip),
        ScenarioPreset("background_foreground_resume", "播放中按 Home 鍵切到背景,幾秒後回到前景恢復播放,測試背景/前景切換的復原能力。", TIER_REGRESSION, background_foreground_resume),
        ScenarioPreset("app_switch_cycle", "播放中連續使用「最近使用 App」鍵切換,測試多工切換穩定度。", TIER_REGRESSION, app_switch_cycle),
        ScenarioPreset("pip_minimize", "進入畫中畫模式並拖曳懸浮視窗。", TIER_REGRESSION, pip_minimize),
        ScenarioPreset("multi_video_session", "連續觀看多支影片(進入影片 A → 返回 → 影片 B → 返回 → 影片 C),測試長時間連續切換的穩定度。", TIER_REGRESSION, multi_video_session),
        *(
            ScenarioPreset(f"play_{video.key}", f"直接開啟並播放{video.title},固定內容、可重現的效能測試情境,適合作為 regression 比對基準。",
                            TIER_REGRESSION, _play_named_video(video))
            for video in NAMED_VIDEOS
        ),
    )
}


def list_scenarios(tier: str | None = None) -> list[str]:
    return sorted(name for name, preset in REGISTRY.items() if tier is None or preset.tier == tier)


def describe_scenarios(tier: str | None = None) -> list[dict]:
    return [
        {"name": preset.name, "description": preset.description, "tier": preset.tier}
        for preset in sorted(REGISTRY.values(), key=lambda p: p.name)
        if tier is None or preset.tier == tier
    ]


def build(name: str, screen: tuple[int, int]) -> list[ScenarioStep]:
    try:
        preset = REGISTRY[name]
    except KeyError:
        raise ValueError(f"Unknown YouTube scenario: {name!r}") from None
    return preset.build(screen)
