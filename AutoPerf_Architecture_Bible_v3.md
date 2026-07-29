# AutoPerf Architecture Bible v3

> **As-built, updated 2026-07-29.** Earlier revisions of this document
> described the intended design; several parts were never built, and several
> built parts were never written down. Every section below now marks what
> exists (with the module that holds it) versus what is still planned, so this
> can be trusted as a description of the system rather than of an intention.

## Vision

AutoPerf is a pluggable mobile automation performance testing framework.
YouTube is only the first adapter.

## Design Principles

-   Framework first
-   Offline-first
-   Plugin architecture
-   Long-running reliability
-   Batch persistence
-   Recoverable after interruption

## Overall Architecture

``` text
Vue Dashboard                          autoperf CLI
      │                                      │
REST + polling                               │
      │                                      │
Django API ── Celery ── Redis                │
      │          │                           │
      └──────────┴───────────────────────────┘
                 │
========================= framework core (src/autoperf) ==
Campaigns          campaigns.py
Test Runner        runner.py
Adapter            adapters.py
ADB                adb.py
Collectors         collectors.py
Metrics Queue      storage.BatchWriter
Batch Writer       storage.BatchWriter
SQLite (WAL)       storage.Storage
Analyzer           analyzer.py
Baseline           storage.baselines + analyzer.compare
==========================================================

Live device screen: webapp/livescreen/server.py -- a standalone asyncio
process piping `adb exec-out screenrecord` over a WebSocket. It never
touches Storage, so it is outside the core stack above.
```

The CLI and the Django API are two front ends over one core. Neither is
privileged: anything the dashboard can start, the CLI can start, with no
Django, Celery or Redis present.

## Core Components

### Test Runner — `runner.py`

Lifecycle, checkpoints, fault isolation, cancellation.

Collectors run in a bounded thread pool; a stuck collector is recorded as a
timeout and cannot block other sampling. Collector and adapter failures become
events rather than ending the run. Three independent cadences: a ~10ms control
tick (timeouts, scenario-step scheduling), a 1s checkpoint heartbeat
(`heartbeat_interval`), and a 1s remote-cancel poll. The heartbeat is separate
from the control tick specifically so checkpoint cost does not scale with run
length — writing one per tick starved the batch writer on multi-hour runs.

Retry and fault *injection*, both named in earlier revisions, are **not built**.

### Campaigns — `campaigns.py`

Long-running test programmes that spawn many ordinary runs: `soak` (one
continuous run of hours) and `repeat` (the same scenario or tier run N times).
Child runs are plain `test_runs` rows carrying a `campaign_id`, so every
existing feature keeps working on them unchanged.

Owns planning, validation, a synchronous executor, cancellation, and the
repeat/soak analyses. Two dispatch strategies sit on top: the CLI runs the
in-process loop, the dashboard pre-enqueues one Celery task per child run.
Campaign status is therefore *derived from child runs on read*, not owned by
an orchestrator — which is what lets both front ends agree.

### Scheduler

**Not a separate component.** Scheduling lives inside `runner.py`: each
collector declares its own `interval` (cpu 1s, memory 5s, battery 10s) and the
runner dispatches on it. There is no 60s tier and no cron-like layer.

### Device Manager

**Not a separate module.** Device discovery and identity are split between
`adb.py` (`devices()`, the safe subprocess boundary) and
`dashboard/services.py` (refresh, nickname, mDNS discovery, adb-over-WiFi
pairing). Capability detection is `adapters.select_adapter()`. Automatic
reconnect is **not built**.

### Adapter — `adapters.py`

`AndroidAdapter` (generic AOSP: `am` / `monkey` / `input`) and
`AndroidTvAdapter` (maps phone scenarios onto TV packages and DPAD input).
`select_adapter()` chooses between them from `ro.build.characteristics`.

No OEM-specific adapter exists. The intended hook point is subclassing
`AndroidAdapter` once a *real, observed* behavioural difference appears —
deliberately not pre-created as empty subclasses.

### Collectors — `collectors.py`

Built: CPU (`cpu.total`, `cpu.user`, `cpu.kernel`), Memory (`memory.used`),
Battery (`battery.level`, `battery.temperature`).

Not built: Network, Logcat, FrameStats.

### Persistence — `storage.py`

SQLite in WAL mode with a single writer queue (`BatchWriter`), letting the
dashboard read concurrently with the CLI's writer.

Aggregation is pushed into SQL rather than done in Python:
`aggregate_samples()` reduces a run to per-metric stats in O(distinct metrics)
memory, and `downsample_samples()` buckets a series to a fixed point budget for
charting. Both exist because loading a multi-hour run's ~100k rows into Python
was not merely slow — the previous 100k row limit silently dropped the *tail*
of a long run, which is exactly where a leak shows.

### Analyzer — `analyzer.py`

Built: per-metric stats, baseline-vs-candidate comparison against a threshold
(`compare`), and trend (`compute_trend` — least-squares slope per hour plus
first-vs-last drift). Trend exists because the mean is near-blind to slow
leaks: 2.0 GB climbing to 2.6 GB over eight hours averages under a 20%
threshold while the device ends 30% worse off.

Baselines are scoped per `(device, scenario)`; comparing across scenarios
produced meaningless deltas, since a heavier scenario legitimately uses more
CPU than a lighter one.

### Report

**Not built.** No Markdown, HTML, PDF or CSV export exists. The dashboard's
stats page and `autoperf campaign show` are currently the only reporting
surfaces.

## Runtime Flow

1.  User starts a run or campaign, from the dashboard or the CLI.
2.  The run row is created immediately, so it is visible before work begins.
3.  Dashboard: Celery enqueues it. CLI: the process runs it directly.
4.  Test Runner executes independently of any UI.
5.  Adapter drives the device; Collectors sample it.
6.  The bounded queue buffers samples.
7.  Batch writer commits them to SQLite.
8.  Analyzer computes verdicts on read, not during the run.
9.  Dashboard polls for live status; the live-screen server streams the
    device's screen separately.

Report generation, step 10 in earlier revisions, does not exist.

## Communication

Control: REST. Live status: polling. Live screen: WebSocket (implemented, but
as its own asyncio process, not Django Channels). SSE was never built and
polling proved sufficient. Raw metrics stay inside the framework.

## Database

Actual tables (`storage.SCHEMA`):

| Table | Holds |
|---|---|
| `devices` | serial, model, nickname, battery, connection, free-form `extra_info` JSON |
| `test_runs` | one row per run; `youtube_scenario`, `campaign_id`, `cancel_requested`, checkpoint |
| `metric_samples` | every sample; the large table |
| `test_events` | lifecycle, collector/adapter errors and timeouts |
| `baselines` | one row per `(device, scenario)` |
| `campaigns` | soak/repeat programmes; child runs point back via `campaign_id` |

`test_cases`, `test_results` and `failure_artifacts` appeared in earlier
revisions and were **never created**. `campaigns` occupies roughly the position
`test_cases` was meant to, though it describes a *programme of runs* rather
than a reusable case definition.

Schema migrations are idempotent `ALTER TABLE` blocks in `Storage.initialize()`
guarded by `PRAGMA table_info` checks, so an existing database upgrades in
place on next open.

## Demo

Windows laptop + Samsung phone + Google TV.

Run test → phone controlled → live metrics + live screen → baseline
comparison → soak/repeat campaign verdict.

## Roadmap

| Version | Scope | Status |
|---|---|---|
| v0.1 | ADB + SQLite | Done |
| v0.2 | Dashboard | Done |
| v0.3 | Adapter | Done (generic + TV; no OEM adapter) |
| v0.4 | Baseline | Done |
| v0.5 | Google TV | Done |
| v0.6 | Redis / Celery | Done |
| v0.7 | AI Report | Not started — no report layer of any kind yet |
| v1.0 | Device Farm | Not started |

Unversioned additions since: live device screen, run replay, Mission Control,
i18n, and long-run campaigns.

## Known Gaps

- No report export in any format.
- No automatic device reconnect.
- No scheduled/recurring execution (no Celery beat); campaigns must be started
  by hand from the CLI or dashboard.
- Network, Logcat and FrameStats collectors unwritten.
- Run replay requires `ffmpeg` on PATH and is skipped silently without it.
- Audio is never captured; `adb screenrecord` cannot.

## Philosophy

The Dashboard is not the Framework.

The Framework must continue collecting data even if the UI closes — and must
be fully drivable without the UI ever having existed.

Focus on reliable data generation, persistence, and recovery.
