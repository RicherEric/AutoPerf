# AutoPerf

AutoPerf v0.1 is an offline-first Android performance test core. It runs independently of a dashboard, samples Android devices through ADB, and persists metrics through a single batch writer into SQLite WAL.

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
different screen resolutions. `scenarios/youtube.py` has 19 named presets (cold
start, search+play, home feed scroll, Shorts browsing, quality switch, like,
comment scroll, fullscreen, seek/scrub, long-press skip, background/foreground
resume, app-switch cycling, PiP, multi-video session, subscriptions/library
browsing) built purely from `launch_app`/`stop_app`/`tap`/`swipe`/`key_event` --
no playback-correctness verification is included by design; these only drive
the UI, the same way `--app` does.

Each preset has a `name`, a Traditional Chinese `description` of exactly what it
does (shown in the dashboard's scenario dropdown as a tooltip and hint text, and
in `autoperf youtube-scenarios list`), and a `tier`:

| Tier | Meaning | Presets |
|---|---|---|
| `smoke` | Fast, fundamental checks -- does the app even launch and reach basic content. Run often. | `cold_start`, `cold_start_and_stop`, `search_and_play`, `home_feed_scroll` |
| `functional` | Common everyday interactions. | `home_feed_tap_video`, `like_video`, `shorts_browsing`, `shorts_like_and_next`, `subscriptions_feed_browse`, `library_and_downloads_browse`, `comment_scroll`, `fullscreen_toggle_cycle` |
| `regression` | Deeper/edge-case flows, worth a slower cadence (e.g. nightly) precisely because they take longer and touch less-common paths. | `quality_switch_manual`, `seek_scrub_forward`, `seek_long_press_skip`, `background_foreground_resume`, `app_switch_cycle`, `pip_minimize`, `multi_video_session` |

```powershell
autoperf youtube-scenarios list --tier smoke
autoperf run-suite --serial <DEVICE_SERIAL> --tier smoke --duration 30
```
`run-suite` runs every scenario in a tier as its own separate run (own run_id,
own report) back to back. The dashboard's Run List page has the same thing as
"Run \<tier\> suite" buttons, backed by `POST /api/suites`.

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
In a second terminal, start the Celery worker (`--pool=solo` is required on
Windows -- Celery's default prefork pool isn't supported there, and `solo`
also happens to satisfy the same "main thread of the main interpreter"
constraint `TestRunner.run()`'s SIGINT handling needs):
```powershell
cd webapp
..\venv\Scripts\celery.exe -A config worker --pool=solo -l info
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

The dashboard has three pages (teal/cyan theme in `frontend/src/theme.css`,
palette validated against the project's dataviz method -- see that file's
header comment):

- **Run List** (`/`) -- device picker, duration, a YouTube scenario dropdown
  grouped by tier (with each option's description as a tooltip), "Run \<tier\>
  suite" buttons, and the run history table.
- **Run Detail** (`/runs/:id`) -- one small SVG line chart per metric
  (`components/MetricChart.vue`; each metric gets its own y-axis since
  cpu %, memory KiB, and battery °C don't share a scale), a "set as
  baseline" button, and a baseline comparison table with a `DeltaBar` per row.
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
available or the H.264 path fails. View-only for now -- tapping the preview
does not drive the device. One stream at a time; a new connection cancels
whatever was previously streaming.
