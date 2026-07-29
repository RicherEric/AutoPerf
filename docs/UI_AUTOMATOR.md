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

**4. Account state and entitlements — not fixable at all.** Two scenarios
cannot work on this account no matter what the selector table says:

- `library_and_downloads_browse` looks for Downloads, which is a **Premium
  entitlement**. On this free account the entry does not exist — the screen
  offers 觀看記錄 / 稍後觀看 / 你的影片 / 喜歡的影片 and an "升級至 Premium"
  upsell instead.
- `subscriptions_feed_browse` expects a subscription feed, but the account
  subscribes to nothing, so YouTube shows a channel-suggestion screen
  (`訂閱「Domingo Ayala」。`…) rather than videos.

This is the category worth knowing about: **a scenario can be perfectly
written and still be meaningless on a given account.** No amount of selector
work fixes it; it needs the right account, or the scenario has to be dropped
for that device. Preflight surfaces it in a couple of minutes rather than after
an hour of measurement.

`downloads_row` now lists the always-present library entries after the
Downloads candidates, so a Premium account still opens Downloads and a free one
still performs the same library-to-detail navigation — and the recorded
strategy says which happened.

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
