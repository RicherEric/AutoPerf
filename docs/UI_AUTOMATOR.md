# UI Automator on phone vs TV

Measured on real hardware, 2026-07-29. Everything below is observed output,
not inference.

| | Galaxy A55 | Chromecast (Google TV) |
|---|---|---|
| Model / OS | SM-A5560, Android 15 | sabrina, Android 14 |
| Locale | `zh-Hant-TW` | `zh-Hant-TW` |
| `ro.build.characteristics` | `phone\n` | `tv,nosdcard\n` |
| Adapter selected | `AndroidAdapter` | `AndroidTvAdapter` |
| YouTube package | `com.google.android.youtube` 21.29.366 | `com.google.android.youtube.tv` |
| Screen | 1080 × 2340 | 1920 × 1080 |

## The finding that matters

**The same app behaves completely differently on the two platforms.**

| YouTube, home screen | Phone | TV |
|---|---:|---:|
| Nodes in hierarchy | **108** | **15** |
| Nodes with a label | **26** | **0** |
| Clickable nodes | **18** | **0** |
| Unobfuscated resource-ids | **49** | 1 (`android:id/content`) |

The phone app is a normal Android view tree: `menu_item_1`, `toolbar`,
`appbar_layout`, `browse_fragment_layout_coordinator_layout`, labelled
accessibility descriptions on every control.

The TV app is **one fullscreen `android.view.View`** and nine 10×10
placeholder views in the top-left corner. Unchanged after 10 seconds, so it is
not a loading artefact. It renders its entire interface to a custom surface,
exposing nothing to the accessibility layer that UI Automator reads.

### This is the app, not the platform

The same Chromecast, same `uiautomator dump`, other apps:

| App | Nodes | Labelled | Clickable | Real ids |
|---|---:|---:|---:|---:|
| Disney+ | 132 | 33 | 18 | **91** |
| TV Settings | 76 | 20 | 11 | yes |
| Google TV launcher | 103 | yes | yes | 0 (all obfuscated) |
| iQIYI | 14 | 4 | 3 | 8 |
| **YouTube TV** | **15** | **0** | **0** | 1 |
| **Netflix** | **6** | **0** | **0** | 4 |

Disney+ is fully introspectable on the very device where YouTube is opaque.
UI Automator works fine on Google TV; YouTube and Netflix specifically render
outside it.

## What this means per platform

### Phone — selectors work, and they are worth having

All five home-screen targets resolve by selector against a real build:

```
search_icon        content_desc  @(1012, 156)   "搜尋"
shorts_tab         content_desc  @(324, 2137)   "Shorts"
subscriptions_tab  content_desc  @(756, 2137)   "訂閱內容"
library_tab        content_desc  @(972, 2137)   "個人中心"
home_feed_video    structural    @(540, 786)
```

Two things this run settled:

- **The Traditional Chinese labels are load-bearing.** `search_icon` matched
  on selector **#1** (`搜尋`) and `subscriptions_tab` on **#1** (`訂閱內容`) —
  the English entries ahead of them missed. An English-only table would have
  dropped both to coordinates on this device.
- **`resource-id` is not reliably available.** Every id in the Google TV
  launcher came back as `0_resource_name_obfuscated`; the release build's
  shrinker strips resource names. Ordering `content-desc` ahead of
  `resource-id` was originally justified by translation pressure; the real
  reason is stronger than that.

### TV — coordinates are not the answer, because they do not exist

Google TV has no touchscreen. `input tap X Y` does nothing, which is why
`AndroidTvAdapter` maps `tap` onto `KEYCODE_DPAD_CENTER`. The model is *move
focus, then press select* — there is no position to verify.

So for YouTube on TV, all three UI-driven strategies are unavailable at once:
no selectors (nothing in the hierarchy), no readable focus (same reason), and
no coordinates (no touchscreen). `AndroidTvAdapter.tap_element` therefore
raises rather than falling back — pressing select blindly is what produced
meaningless TV runs before.

### TV — what does work: deep link plus outcome verification

Verified on both devices, same scenario, no UI introspection involved:

```
t+0.0   launch_app         OK      https://www.youtube.com/watch?v=TlFrIH6GQhk
t+3.0   verify_foreground  OK      com.google.android.youtube.tv
t+8.0   verify_playing     OK      verified: True
```

`dumpsys media_session` reports the state on both platforms, and on the
Chromecast even confirms *which* video is playing:

```
state=PlaybackState {state=PLAYING(3), position=37, speed=1.0, ...}
metadata: description="Golden" Official Music Video | KPop Demon Hunters
```

A deep link addresses content by URL — a public contract YouTube cannot break
without breaking the web — and `verify_playing` confirms the outcome from a
platform API rather than from the app's UI. Neither depends on anything the TV
app declines to expose. **The four `play_*` presets are the only scenarios in
the library that are fully sound on TV**, and they are also the ones best
suited to baseline comparison, since their content is fixed.

## Measured: the search-and-tap entry is not reproducible

`_enter_video_steps` — the opening used by 11 of the 24 presets — launches
YouTube and issues four blind coordinate taps: search icon, search bar, first
suggestion, result thumbnail. It never types anything, so "first suggestion"
is whatever search history or trending happens to offer.

Run three times on the Galaxy A55 over USB, nothing changed in between:

| Run | `is_playing` | What actually played |
|---|---|---|
| 1 | `True` | "Golden" Official **Lyric** Video |
| 2 | `True` | "Golden" Official **Lyric** Video |
| 3 | `True` | 2026年7月必聽新歌 \| KKBOX華語單曲排行週榜 (1h+ compilation) |

Same scenario, three runs, **two different videos**. And the activity stayed
`Shell$HomeActivity` throughout — the search screen never opened at all. The
taps did not do what the scenario claims; they landed on the home feed and
opened whatever happened to sit at that position.

The deep-link presets, same device, same three-run treatment:

| Run | `is_playing` | What actually played |
|---|---|---|
| 1–3 | `True` | "Golden" Official **Music** Video (the requested `TlFrIH6GQhk`) |

**Both methods report success every time.** That is precisely the problem: the
old code saw a completed run with a full set of metrics and recorded a healthy
`search_and_play`. But run 3 decoded an hour-long compilation while runs 1 and
2 decoded a short lyric video — an order of magnitude apart in decode and
render load.

So a baseline comparison built on `search_and_play` can differ between runs
entirely because the *content* differed, not because the device changed. The
README previously asserted that fixed content matters for baselines; this is
the measurement behind the assertion.

It does not make the search-and-tap presets worthless — they exercise a real
user path, and that is worth measuring. It makes them the wrong tool for
baseline comparison, which is what the `play_*` presets are for.

### The full preflight showed the flow was broken outright

Running the whole covering set — 11 scenarios, 22 targets — produced a result
that only looked like nine separate selector problems:

```
FELL BACK   first_suggestion  0/7   comments_row  fullscreen_enter/exit
                              like_button  pip_caret  quality_row/option
                              shorts_like_button
FAILED      7 scenarios: expected active media playback, found none
```

One root cause. `first_suggestion` matched nothing on **7 of 7** attempts,
because the flow tapped the search box and then tapped where a suggestion
would be *without ever typing anything* — so the suggestion list was empty.
The flow never reached a video, which is why every player control
(quality, fullscreen, like, comments, PiP) was equally absent: there was no
player. What the observed screens actually showed at those moments was a
minimised player bar and a search-results page.

Before verification existed, all seven of those scenarios passed.

**The fix is to type.** `input text` is a keystroke injector — it depends on
nothing being introspectable, so it works wherever a field has focus, exactly
like a deep link works wherever the app is installed. The flow now types a
fixed `SEARCH_QUERY` and presses Enter.

Measured on the same phone, three runs each:

| | Before | After |
|---|---|---|
| Search screen | never opened | opens and receives the query |
| Distinct videos across 3 runs | **2** | **1** |
| Step failures | `verify_playing` × 3 | **0** |

`verify_playing` also had to stop sampling and start waiting. A run that had
genuinely reached the right video still read as not-playing two seconds after
the tap, because a livestream was still buffering — the same class of mistake
as a fixed-time tap, one layer up.

The presets remain unsuitable for baseline comparison: search results shift
over time, which is what the `play_*` deep links are for. They now do what
their names say.

## Preflight on real hardware found three different kinds of failure

Running `autoperf preflight` against the Galaxy A55 over USB surfaced findings
that had been indistinguishable from one another — all of them would previously
have shown up as "the selector is wrong", and only one of them was.

**1. Selector decay — fixable in the table.** `library_tab` listed `"你"`,
which matched nothing useful. The real label is `個人中心`.

**2. Arriving too early — fixable in code.** `subscriptions_feed_browse` taps
the Subscriptions tab at t=3.0s. At that moment YouTube has rendered its
containers but not its bottom navigation, so a single-shot lookup found
nothing and reported a decayed selector. `ElementActionsMixin` now retries a
few times before giving up.

**3. State leaking between scenarios — fixable in preflight.** After
`home_feed_tap_video` opened a video, the next scenario's `launch_app` merely
returned the already-running app to the foreground, still on the watch screen,
so its bottom-navigation target was legitimately absent. Real runs never hit
this because each is a separate `TestRunner.run()` that force-stops the app at
the end; preflight now does the same between scenarios.

**4. Account state — not fixable in the selector table.** Signed out, two
scenarios could not work at all no matter what the table said:

- `library_and_downloads_browse` found no downloads entry anywhere, scrolled
  or not. The screen showed 觀看記錄 / 稍後觀看 / 你的影片 / 喜歡的影片 and an
  "升級至 Premium" upsell.
- `subscriptions_feed_browse` got a channel-suggestion screen
  (`訂閱「Domingo Ayala」。`…) instead of a feed, because the account
  subscribed to nothing.

Signing in resolved both. The downloads entry appeared as **「已下載的內容」**
— a label none of the guesses matched — and it sits **below the fold**, so the
scenario now scrolls before reaching for it. Tapping straight after opening the
tab found nothing however good the selector was.

(An earlier draft of this document called Downloads a Premium-only entitlement.
That was inferred from its absence while signed out and is not supported by the
evidence: the entry appeared on signing in, and whether Premium additionally
gates it was never tested.)

The category still holds even though this instance was fixable: **a scenario
can be perfectly written and still be meaningless on a given account.**
Preflight surfaces that in a couple of minutes rather than after an hour of
measurement.

`downloads_row` lists the always-present library entries after the downloads
candidates, so an account without downloads still performs the same
library-to-detail navigation the scenario measures rather than dropping to a
blind coordinate — and the recorded strategy says which one matched.

**Feed timing is network-dependent, and fixed step times are therefore
inherently flaky.** `subscriptions_feed_browse` passes when run alone and
sometimes falls back when run after other scenarios, because the feed is
re-fetched and a fixed t=9.5s tap can arrive first. The retry in
`ElementActionsMixin` absorbs some of this; the rest is exactly what a repeat
campaign's flaky rate exists to quantify.

## Three bugs this session found

All three were invisible without real hardware, and all three are the same
failure mode: something reporting success while doing nothing.

**1. `verify_playing` never actually verified anything.** The parser handled
only `state=3`, but current Android emits `state=PLAYING(3)`. Every modern
device returned "unknown", so the check could never fail — the strongest
assertion in the system was silently inert. Fixed to accept both forms;
confirmed `True` on both devices.

**2. TV runs were all marked unverified.** Scenarios name the phone package
and the TV adapter substitutes `.tv` when launching, but `verify_foreground`
compared against the unsubstituted name, so it failed unconditionally on
healthy runs. Verification now applies the same mapping as launching.

**3. A selector matched the wrong control and reported success.** The Library
tab listed `"你"` as a candidate label. Being a single character, substring
matching found it inside a video title (`沒有聽完是你的損失`) and resolved
confidently to that video's overflow-menu button. A wrong match is worse than
no match: it taps something real and looks like it worked. Labels of two
characters or fewer now require an exact match
(`uiauto.SHORT_LABEL_LENGTH`), and the real label turned out to be
`個人中心`.

The measured bottom-navigation coordinates were also wrong by a whole tab —
`shorts_tab`'s guess landed on the Create button — and have been replaced with
captured values.

## Practical guidance

- **Phone:** selectors work. Run `autoperf preflight --serial <SERIAL>` after
  any YouTube update; it reports which entries in
  `src/autoperf/scenarios/selectors.py` decayed and what is on screen instead.
- **TV:** use the `play_*` deep-link presets. Treat the UI-driven presets as
  phone-only; they will fail loudly on TV, which is the intended behaviour.
- **Either:** `uiautomator dump` waits for an idle UI and a playing video
  never is, so taps issued during playback degrade to coordinates fairly
  often. Expected, visible in `selector_fallback` events.
- Pin the app version. Selectors cannot decay until it changes, and a
  comparison across two app builds measures the app rather than the device.

## Measured on the hardware: what each primitive actually costs

Galaxy A55 (SM-A5560), Android 15, adb over USB, median of five trials.

| Read | Median | Payload | Note |
| --- | --: | --: | --- |
| `wm size` | 147ms | 25 B | cached per adapter now: it cannot change during a run |
| `dumpsys window displays` | 122ms | 25 KB | rotation; re-read every action because rotation moves |
| `dumpsys window` | 141ms | 69 KB | backs `verify_foreground` |
| `dumpsys media_session` | 121ms | 20 KB | backs `verify_playing` |
| `dumpsys cpuinfo` | 118ms | 8.8 KB | |
| `cat /proc/meminfo` | 105ms | 1.6 KB | |
| `dumpsys battery` | 113ms | 11 KB | |
| **`uiautomator dump` + `cat`** | **2611ms** | 27 KB | **20x everything else** |

### The dump is the whole cost, and no transport trick helps

| Strategy | Median | Verdict |
| --- | --: | --- |
| `dump` + `cat` | 2611ms | current |
| `dump --compressed` + `cat` | 2455ms | 6% faster, **27 nodes instead of 72** -- prunes what selectors need |
| `shell uiautomator dump /dev/tty` | 2331ms | returns no XML, only a status line |
| `exec-out uiautomator dump /dev/tty` | 2546ms | appends a status line after the closing tag, does not parse |
| `exec-out dump --compressed /dev/tty` | 2484ms | both of the above problems |

All within 12%: the cost is `uiautomator dump` waiting for an idle UI, not the
transport. **The only optimisation that matters is dumping less often.**

### Cost depends on what is on screen

| Screen | Median | Worst |
| --- | --: | --: |
| launcher, idle | 2.60s | 2.64s |
| YouTube home feed | 2.73s | 2.82s |
| watch page, video playing | 3.01s | **11.66s** |

A playing video is never idle, so the tail is long and unbounded.

### Which is why the retry budget was wrong

`resolve_attempts=3` with a 1s delay is 11.0s at the *median* watch-page cost,
against `TestRunner.adapter_action_timeout` of 10.0s. Measured before the fix, a
`tap_element` for an absent target on a playing watch page took **25.5s** (three
dumps, 23.4s of dumping) -- so the retry that exists to rescue an early arrival
instead got the step killed, which is strictly worse than not retrying.

Now bounded by `resolve_budget` (7.0s) and `dump_timeout` (6.0s), both provably
under the action timeout. Re-measured on the same screen: 6.25s and 3.28s, both
fitting. A slow device gets one attempt and a coordinate fallback; a fast one
still gets all three.

### Launching: the deep link wins twice

| | Command returns | App actually in front |
| --- | --: | --: |
| `monkey -c LAUNCHER` | 506ms | 0.95s |
| `am start -a VIEW -d <watch url>` | 115ms | 0.57s |

Faster *and* deterministic, which is why the `play_*` presets are the right tool
for baseline comparison.

### The like button reports no state

Dumped the node before and after a real tap on the like control:

```
before  desc='和另外 19,258,638 人都喜歡這部影片'  selected=False  checked=False
after   desc='和另外 19,258,651 人都喜歡這部影片'  selected=False  checked=False
```

`selected` and `checked` never move. The only thing that changes is the like
*count* in the label -- global traffic on the video, not this device's tap; it
moved by 13 between two reads seconds apart. So there is nothing to assert on:
`verify_element_state` would report a failure on every run, and a before/after
comparison would read other people's likes as proof of ours. The mechanism is
sound and correct; this build gives it nothing to point at.

### Selector health on the A55 home feed

10 of 22 targets matched by selector; the rest fell to coordinates, and all but
one legitimately -- they belong to screens the feed is not. The exception is
`third_video`: this screen fits two feed rows above the fold, so an index-2
structural selector cannot match without scrolling first.
