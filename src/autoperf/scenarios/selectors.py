"""Where every YouTube UI target is described, and the only file that rots.

Each entry lists how to find one on-screen element, most stable strategy
first, ending with the fractional coordinate that used to be the *only*
strategy. Resolution order and rationale live in `uiauto.Selector`; the short
version is content-desc, then resource-id, then structure, then coordinates.

**The resource-ids and labels below are unverified starting points.** They
cannot be verified without the app in front of you: an app's resource-ids are
internal implementation details its authors have no reason to keep stable,
and they are not published anywhere. Capture the real ones from a device with

    autoperf ui-dump --serial <SERIAL>

while the relevant screen is open, and correct the entries here. Nothing
breaks in the meantime -- an entry whose selectors all miss falls through to
its coordinate, which is exactly the behaviour that preceded this module --
but the run records a `selector_fallback` event so you can see it happening
instead of guessing.

Keeping every target in one table is the point: when a YouTube update moves
the UI, the fix is this file, not nineteen scenario functions. And with the
app version pinned -- which performance regression testing needs anyway, or
the baseline-vs-candidate delta measures the app's change rather than the
device's -- these entries cannot decay until the version is deliberately
bumped.
"""

from __future__ import annotations

from ..uiauto import Selector, Target

PACKAGE = "com.google.android.youtube"


def _id(name: str) -> Selector:
    return Selector(resource_id=f"{PACKAGE}:id/{name}")


def _desc(*labels: str) -> tuple[Selector, ...]:
    """One content-desc selector per label, tried in order.

    Accessibility labels are translated, so a single English string only
    matches an English device -- on a zh-TW phone every one of them would
    miss and the target would silently drop to its coordinate. Listing the
    labels for each locale in use keeps the stable strategy working instead
    of quietly degrading to the fragile one.

    Matching is case-insensitive and substring-based (see `Selector.matches`),
    so "like" also covers "Like this video" and its longer variants.
    """
    return tuple(Selector(content_desc=label, clickable=True) for label in labels)


# --- top-level navigation ---------------------------------------------------

SEARCH_ICON = Target(
    # Confirmed against a real build: the "搜尋" label and `menu_item_1` both
    # match. The English label is kept first for English devices.
    selectors=(
        *_desc("Search", "搜尋"),
        _id("menu_item_1"),
        _id("search_button"),
    ),
    fallback=(0.937, 0.067),
    name="search_icon",
)

SEARCH_BAR = Target(
    selectors=(
        _id("search_edit_text"),
        Selector(class_name="EditText"),
        *_desc("Search YouTube", "搜尋 YouTube"),
    ),
    fallback=(0.5, 0.08),
    name="search_bar",
)

# Bottom-navigation coordinates below are measured, not guessed: captured
# from a Galaxy A55 (1080x2340, zh-Hant-TW, YouTube 21.29.366). The values
# they replace were off by a whole tab -- shorts_tab's old (0.6, 0.95) landed
# on the Create button, and subscriptions_tab's landed between two tabs.
SHORTS_TAB = Target(
    selectors=(*_desc("Shorts"), _id("pivot_shorts")),
    fallback=(0.30, 0.913),
    name="shorts_tab",
)

SUBSCRIPTIONS_TAB = Target(
    selectors=(*_desc("Subscriptions", "訂閱內容", "訂閱"), _id("pivot_subscriptions")),
    fallback=(0.70, 0.913),
    name="subscriptions_tab",
)

LIBRARY_TAB = Target(
    # "個人中心" is what a real zh-Hant-TW build labels this tab; the guesses
    # it replaces ("你", "媒體庫") matched nothing. The bare "你" was worse than
    # useless -- being a single character, substring matching found it inside a
    # video title and resolved confidently to that video's overflow button.
    # See uiauto.SHORT_LABEL_LENGTH for the guard that now prevents it.
    selectors=(
        *_desc("個人中心", "You", "Library", "媒體庫"),
        _id("pivot_library"),
    ),
    fallback=(0.90, 0.913),
    name="library_tab",
)


# --- lists: structural, because "some video" is the actual intent -----------

def _feed_item(index: int, fallback: tuple[float, float], name: str) -> Target:
    """The index-th tappable row in a feed.

    Structural rather than identified: these scenarios want *a* video, not a
    particular one, so "the second clickable row" both survives renames and
    says what the scenario means. `min_area` filters out the small clickable
    chrome (avatars, overflow dots) that would otherwise be picked first.
    """
    return Target(
        selectors=(
            Selector(class_name="ViewGroup", clickable=True, index=index, min_area=200_000),
            Selector(clickable=True, index=index, min_area=200_000),
        ),
        fallback=fallback,
        name=name,
    )


FIRST_SUGGESTION = Target(
    selectors=(_id("suggestion_text"), Selector(class_name="TextView", clickable=True, index=0)),
    fallback=(0.5, 0.2),
    name="first_suggestion",
)

RESULT_THUMBNAIL = _feed_item(0, (0.5, 0.35), "result_thumbnail")
HOME_FEED_VIDEO = _feed_item(0, (0.5, 0.45), "home_feed_video")
SUBSCRIPTION_VIDEO = _feed_item(0, (0.5, 0.45), "subscription_video")
SECOND_VIDEO = _feed_item(1, (0.5, 0.45), "second_video")
THIRD_VIDEO = _feed_item(2, (0.5, 0.6), "third_video")
DOWNLOADS_ROW = Target(
    # Captured label: "已下載的內容". Two things about it are only learnable
    # from a device. It sits *below the fold* on the 個人中心 screen, so the
    # scenario scrolls before reaching for it -- tapping without scrolling
    # found nothing however good the selector was. And it is gated on being
    # signed in: the same phone signed out showed an "升級至 Premium" upsell
    # and no downloads entry anywhere, scrolled or not.
    #
    # The always-present library entries follow it, so a signed-out or
    # otherwise-limited account still performs the same library-to-detail
    # navigation the scenario exists to measure rather than dropping to a
    # blind coordinate. The recorded strategy says which one matched.
    selectors=(*_desc("已下載的內容", "Downloads", "下載內容"),
               Selector(text="Downloads", clickable=True),
               *_desc("觀看記錄", "Watch history"),
               *_desc("稍後觀看", "Watch later"),
               _id("downloads_entry")),
    fallback=(0.5, 0.3),
    name="downloads_row",
)


# --- player controls --------------------------------------------------------

PLAYER_SURFACE = Target(
    selectors=(_id("player_view"), _id("watch_player")),
    fallback=(0.5, 0.5),
    name="player_surface",
)

LIKE_BUTTON = Target(
    selectors=(
        *_desc("like this video", "我喜歡這部影片", "喜歡"),
        _id("like_button"),
    ),
    fallback=(0.15, 0.62),
    name="like_button",
)

SHORTS_LIKE_BUTTON = Target(
    selectors=(*_desc("like this video", "我喜歡這部影片", "喜歡"), _id("reel_like_button")),
    fallback=(0.9, 0.55),
    name="shorts_like_button",
)

COMMENTS_ROW = Target(
    selectors=(*_desc("Comments", "留言"),
               Selector(text="Comments"), Selector(text="留言"),
               _id("comments_entry_point")),
    fallback=(0.5, 0.68),
    name="comments_row",
)

OVERFLOW_MENU = Target(
    selectors=(
        *_desc("More options", "更多選項", "更多"),
        _id("player_overflow_button"),
    ),
    fallback=(0.95, 0.4),
    name="overflow_menu",
)

QUALITY_ROW = Target(
    selectors=(*_desc("Quality", "畫質"),
               Selector(text="Quality", clickable=True), Selector(text="畫質", clickable=True),
               _id("quality_menu_item")),
    fallback=(0.5, 0.55),
    name="quality_row",
)

QUALITY_OPTION = Target(
    selectors=(Selector(class_name="TextView", clickable=True, index=1),),
    fallback=(0.5, 0.4),
    name="quality_option",
)

FULLSCREEN_ENTER = Target(
    selectors=(*_desc("Enter fullscreen", "全螢幕"), _id("fullscreen_button")),
    fallback=(0.93, 0.58),
    name="fullscreen_enter",
)

FULLSCREEN_EXIT = Target(
    selectors=(*_desc("Exit fullscreen", "結束全螢幕", "退出全螢幕"), _id("fullscreen_button")),
    fallback=(0.93, 0.9),
    name="fullscreen_exit",
)

PIP_CARET = Target(
    selectors=(*_desc("Collapse", "Minimize", "收合", "縮小"), _id("player_collapse_button")),
    fallback=(0.06, 0.42),
    name="pip_caret",
)


ALL_TARGETS: tuple[Target, ...] = tuple(
    value for name, value in sorted(globals().items())
    if isinstance(value, Target)
)
