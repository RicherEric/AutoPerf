# AutoPerf

AutoPerf v0.1 is an offline-first Android performance test core. It runs independently of a dashboard, samples Android devices through ADB, and persists metrics through a single batch writer into SQLite WAL.

Works on both Windows and macOS -- everything talks to the phone through
`adb`, which ships for both platforms. See [`docs/INSTALL.md`](docs/INSTALL.md)
for full manual + automated (`scripts/setup.ps1` / `scripts/setup.sh`) setup
instructions covering both. The quick start below assumes Windows; swap
`.\venv\Scripts\python.exe` for `./venv/bin/python` on macOS.

## Quick start (Windows PowerShell)

```powershell
.\venv\Scripts\python.exe -m pip install -e .
adb devices
autoperf devices
autoperf run --serial <DEVICE_SERIAL> --duration 60
autoperf run --serial <DEVICE_SERIAL> --duration 60 --app com.android.settings
autoperf run --serial <DEVICE_SERIAL> --duration 60 --youtube-scenario cold_start
autoperf youtube-scenarios list
autoperf run-many --serial <DEVICE_A> --serial <DEVICE_B> --duration 60
autoperf status <RUN_ID>
```

The initial collectors are total CPU, used memory, battery level, and battery temperature. Collector failures are recorded as events and do not stop the run. Pressing Ctrl+C records an interrupted checkpoint; resume with `autoperf run --serial <DEVICE_SERIAL> --duration 60 --resume <RUN_ID>`.

Collectors execute concurrently in a bounded thread pool. A stuck collector is reported as a timeout and cannot block other sampling schedules. Multi-device runs use one Windows `spawn`-compatible worker process per device; the supervisor records worker crashes and heartbeat timeouts.

The optional `--app <package>` flag drives the device via an `Adapter` (see `adapters.py`) instead of only sampling it: it launches the given package at the start of the run and lets it sit foregrounded while collectors keep sampling. Adapter actions are recorded as `adapter_action`/`adapter_error`/`adapter_timeout` events, symmetric to how collector failures are recorded.

## Architecture boundaries

- `adb.py`: safe subprocess boundary and device discovery
- `collectors.py`: plug-in sampling interface
- `adapters.py`: plug-in device-control interface (launch/stop app, tap, swipe, key event)
- `runner.py`: lifecycle, scheduling, fault isolation, checkpoints
- `storage.py`: WAL schema and single writer queue
- `analyzer.py`: per-metric mean/stdev/min/max and baseline-vs-candidate comparison
- `scenarios/`: relative-coordinate helpers (`coords.py`) and the YouTube preset library (`youtube.py`)
- `screen_stream.py`: pure Annex-B NAL splitter / access-unit assembler used by the live-screen server
- `cli.py`: headless control surface (`devices`, `run`, `run-many`, `status`, `baseline set/show`, `compare`, `youtube-scenarios list`)

Run tests without third-party dependencies: `python -m unittest discover -s tests -v`.

## YouTube scenario library

`adapters.py`'s `screen_size()` (parses `adb shell wm size`) lets `scenarios/coords.py`
express taps/swipes as fractions of the screen (0..1) instead of hardcoded pixels,
resolved to absolute coordinates once per run -- so the same scenario works across
different screen resolutions. `scenarios/youtube.py` has 23 named presets (cold
start, search+play, home feed scroll, Shorts browsing, quality switch, like,
comment scroll, fullscreen, seek/scrub, long-press skip, background/foreground
resume, app-switch cycling, PiP, multi-video session, subscriptions/library
browsing) built purely from `launch_app`/`stop_app`/`tap`/`swipe`/`key_event` --
no playback-correctness verification is included by design; these only drive
the UI, the same way `--app` does.

Four of those (`play_golden`, `play_baby_groot_dancing`, `play_suis_moi`,
`play_rickroll`) deep-link straight to a specific, known YouTube video via
`launch_app`'s `data` param (`am start -a android.intent.action.VIEW -d
<url>`) instead of the blind search-and-tap taps the rest use -- replaying
the exact same video every run gives reproducible length/resolution/content,
which matters for baseline-vs-candidate comparisons. `scenarios/youtube.py`'s
`NAMED_VIDEOS` tuple is where to add more of the same theme.

Each preset has a `name`, a Traditional Chinese `description` of exactly what it
does (shown in the dashboard's scenario dropdown as a tooltip and hint text, and
in `autoperf youtube-scenarios list`), and a `tier`:

| Tier | Meaning | Presets |
|---|---|---|
| `smoke` | Fast, fundamental checks -- does the app even launch and reach basic content. Run often. | `cold_start`, `cold_start_and_stop`, `search_and_play`, `home_feed_scroll` |
| `functional` | Common everyday interactions. | `home_feed_tap_video`, `like_video`, `shorts_browsing`, `shorts_like_and_next`, `subscriptions_feed_browse`, `library_and_downloads_browse`, `comment_scroll`, `fullscreen_toggle_cycle` |
| `regression` | Deeper/edge-case flows, worth a slower cadence (e.g. nightly) precisely because they take longer and touch less-common paths. Includes the four deep-linked specific-video presets -- their fixed content is exactly what baseline-vs-candidate comparisons need. | `quality_switch_manual`, `seek_scrub_forward`, `seek_long_press_skip`, `background_foreground_resume`, `app_switch_cycle`, `pip_minimize`, `multi_video_session`, `play_golden`, `play_baby_groot_dancing`, `play_suis_moi`, `play_rickroll` |

```powershell
autoperf youtube-scenarios list --tier smoke
autoperf run-suite --serial <DEVICE_SERIAL> --tier smoke --duration 30
```
`run-suite` runs every scenario in a tier as its own separate run (own run_id,
own report) back to back. The dashboard's Run List page has the same thing as
"Run \<tier\> suite" buttons, backed by `POST /api/suites`.

Every run's `test_runs` row records which scenario (if any) drove it
(`youtube_scenario` column) so the stats page can compute a per-scenario pass
rate. The CLI/dashboard pre-create the run row with this field set *before*
calling `TestRunner.run()` with that same run_id -- `TestRunner` sees the row
already exists and skips its own `create_run()`, so no changes were needed in
`runner.py` itself.

## Baseline / regression comparison (v0.4)

```powershell
autoperf baseline set --serial <DEVICE_SERIAL> --run <RUN_ID>
autoperf baseline show --serial <DEVICE_SERIAL>
autoperf compare --run <RUN_ID> --threshold 20
```

`baseline set` designates one run per device as its baseline (`storage.baselines`,
one row per device). `compare` computes per-metric mean/stdev/min/max for both the
baseline and the candidate run and flags any metric whose mean moved beyond
`--threshold` percent in either direction. It doesn't assume whether higher or
lower is "worse" for a given metric name -- that's a judgment call left to
whoever reads `delta_pct`, since a future collector could add a metric where
either direction is fine.

## Dashboard (v0.2) + background workers (v0.6)

A Django + Vue dashboard reads the same `autoperf.db` directly (WAL mode allows
concurrent readers alongside the CLI's writer). Triggering a run enqueues a
Celery task on Redis instead of blocking the request or spawning a raw OS
process directly -- the run row is created immediately (`pending`), and Celery
picks it up on its own worker.

```powershell
.\venv\Scripts\python.exe -m pip install -e .[dashboard,worker]
docker run -d --name autoperf-redis -p 6379:6379 redis:7-alpine
.\venv\Scripts\python.exe webapp\manage.py runserver 8000
```
In a second terminal, start the Celery worker via `scripts/start-worker.py`,
which sizes parallelism to `min(cpu_count, connected_adb_device_count)` and
picks the right pool for the platform (`--pool=solo` is required on
Windows -- Celery's default prefork pool isn't supported there, and `solo`
also happens to satisfy the same "main thread of the main interpreter"
constraint `TestRunner.run()`'s SIGINT handling needs; see
`docs/INSTALL.md`'s "Sizing worker parallelism" section for the full
story, including how same-device runs are still serialized even with
multiple workers):
```powershell
.\venv\Scripts\python.exe scripts\start-worker.py
```
In a third terminal, the frontend:
```powershell
cd webapp\frontend
npm install
npm run dev
```
Open `http://localhost:5173` -- refresh devices, pick one, start a run, and
watch status/metrics update via polling. From a run's detail page you can also
mark it as that device's baseline and see a live comparison against it.

Run the API tests with `python webapp\manage.py test dashboard` -- they call
the Celery task function directly (bypassing `.delay()`), so they need
neither Redis nor a running worker.

The dashboard has five of these pages (teal/cyan theme in `frontend/src/theme.css`,
palette validated against the project's dataviz method -- see that file's
header comment):

- **統計概覽 / Stats** (`/`, the home page) -- overall pass rate, today's run
  count, a per-scenario pass-rate bar list (worst-first), and metric trend
  charts across recent run history. Backed by `GET /api/stats`
  (`services.get_dashboard_stats`), which reuses the exact same baseline
  comparison analyzer.py already does for one run at a time
  (`GET /api/runs/<id>/comparison`) across recent history instead -- a run
  whose device has no baseline yet is its own "no_baseline" bucket, not
  counted as pass or fail.
- **Run List** (`/runs`) -- device picker, duration, a YouTube scenario
  dropdown grouped by tier (with each option's description as a tooltip),
  "Run \<tier\> suite" buttons, and the run history table with a
  `DeviceMoodBadge` emoji per device (derived from `battery_level` --
  CPU%/temperature are per-run metrics, not device properties, so they
  can't drive this outside an active run).
- **Run Detail** (`/runs/:id`) -- one small SVG line chart per metric
  (`components/MetricChart.vue`; each metric gets its own y-axis since
  cpu %, memory KiB, and battery °C don't share a scale), a "set as
  baseline" button, and a baseline comparison table with a `DeltaBar` per row.
  The live screen panel overlays the latest metric values as a HUD on the
  canvas itself, and a toast + short beep fire the moment a metric crosses
  its regression threshold *during* a run (not just afterward in the
  comparison table). Once the run finishes, the live panel is replaced by a
  native `<video controls>` replay if one was recorded (see "Live device
  screen" below).
- **Mission Control** (`/mission-control`) -- every currently-running run at
  once, each as a `LiveScreenPanel` + a `MiniSparkline` of its `cpu.total`,
  polling `GET /api/queue`'s `running_runs`. A grid version of Run Detail's
  live panel for watching several devices run in parallel simultaneously.
- **Task Queue** (`/queue`) -- polls `GET /api/queue`, which wraps Celery's
  `control.inspect()`. No worker replying within the timeout is a normal,
  clearly-labeled state (distinct from the broker itself being unreachable),
  not an error. **Known Celery limitation**: `--pool=solo` is fully
  synchronous, so the worker can't answer an `inspect()` broadcast while it's
  busy executing a task -- `active`/`reserved` will under-report (often to
  zero) for the whole duration of a run. The page also shows a "Currently
  running" table sourced directly from Storage (`list_running_runs()`), which
  has no such blind spot, for exactly this reason.

## Live device screen (view-only)

```powershell
.\venv\Scripts\python.exe -m pip install -e .[livescreen]
cd webapp
..\venv\Scripts\python.exe -m livescreen.server --port 8100
```
Open `/screen` in the dashboard, pick a device, and connect. This runs as its
own standalone asyncio process (not Django Channels -- it never touches
Storage/SQLite, just `adb exec-out screenrecord --output-format=h264 -` piped
over a WebSocket) and decodes in-browser via WebCodecs
(`VideoDecoder`, Annex-B format, Chrome/Edge 94+) with an automatic fallback to
periodic PNG screenshots (`adb exec-out screencap -p`) if WebCodecs isn't
available or the H.264 path fails. `VideoDecoder.configure()` is called with
only `{codec, hardwareAcceleration, optimizeForLatency}` -- no `description`,
no `avc` field -- matching `@yume-chan/scrcpy-decoder-webcodecs` (used by
ws-scrcpy/tango), a real production scrcpy-in-browser implementation: omitting
`description` for an `avc1.*` codec is what signals Annex-B to Chrome. View-only
for now -- tapping the preview does not drive the device. One stream at a time
*per device* (`livescreen/server.py`'s `_active_tasks` is keyed by serial); a
new connection to the *same* device cancels whatever was previously streaming
to it, but watching two different devices at once (e.g. two Run Detail tabs)
doesn't cancel each other.

The same WebCodecs connect/decode/fallback logic is shared (via the
`useDeviceScreen()` composable, `frontend/src/composables/useDeviceScreen.js`)
between the standalone `/screen` page and a `LiveScreenPanel` embedded
directly in Run Detail: while a run is `running`, its device's live screen
connects automatically right next to the metric charts, so you can watch the
screen and the performance graphs update together without a separate tab.

**Run replay (optional, needs `ffmpeg`)**: when a `LiveScreenPanel` is opened
with a `run_id` (Run Detail, Mission Control), the server also tees the raw
H.264 stream into `ffmpeg -c copy -use_wallclock_as_timestamps 1` (a cheap
remux, no re-encode; the wallclock-timestamps flag matters because a raw
H.264 elementary stream carries no PTS/DTS of its own, so without it ffmpeg
has to guess a constant frame rate for the whole file -- which can produce a
wildly wrong, including near-zero, duration) writing
`webapp/recordings/<run_id>.mp4.part`. This deliberately reuses the
*existing* live stream rather than spawning a second, independent
screen-capture session -- Android only allows one per device at a time. The
tradeoff: a run is only recorded for whatever portion of it someone had a
live panel open for (in practice, the whole run, since Run Detail's panel
auto-connects the moment the page opens).

The `.part` file is only renamed to its final `<run_id>.mp4` name once
ffmpeg has actually exited cleanly (`_stop_recording`) -- a reader checking
`GET /api/runs/<id>/recording` for the *final* name can never see a
partially-written file. (Without this, a reader could open the file while
ffmpeg was still mid-write, before its moov atom/trailer existed at all --
the real, empirically-observed cause of a browser `<video>` showing "0:00,
spins forever": the file existed, but had no valid duration or sample index
yet.) Run Detail polls that endpoint a few times, a couple seconds apart,
after a run finishes to give ffmpeg's flush time to actually complete.

Once ready, Run Detail renders a native `<video controls>` -- the browser's
own seek bar is the scrub UI, no custom scrubber needed. If `ffmpeg` isn't
installed, recording is silently skipped (logged once) and live streaming is
unaffected either way. Recordings are served in dev via
`django.views.static.serve` (`RECORDINGS_ROOT`/`RECORDINGS_URL` in
`config/settings.py`), which handles the HTTP Range requests `<video>`
seeking needs. Deleting a run also deletes its recording.

Audio is not captured -- only video. `adb shell screenrecord` (what this
pipes from) has no device-audio capture capability at all; getting YouTube's
actual audio track would need Android's `MediaProjection`
`AudioPlaybackCapture` API (Android 12+), which requires a companion app
running on the device (this is exactly what scrcpy's audio forwarding does)
-- a materially bigger undertaking than the current pure-`adb`-shell
pipeline, not a small addition on top of it.
