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
