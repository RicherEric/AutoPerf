# AutoPerf Architecture Bible v3

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
Vue Dashboard
      │
REST + SSE/Polling
      │
Django Management API
      │
Test Controller
      │
========================
Test Runner
Scheduler
Device Manager
Adapter
ADB
Collectors
Metrics Queue
Batch Writer
SQLite (WAL)
========================
Analyzer
Baseline
Report
```

## Core Components

### Test Runner

Lifecycle, retry, checkpoint, fault injection.

### Scheduler

1s / 5s / 60s jobs.

### Device Manager

Detect devices, capability detection, reconnect.

### Adapter

SamsungAdapter, GoogleTVAdapter, future plugins.

### Collectors

CPU, Memory, Battery, Temperature, Network, Logcat, FrameStats.

### Persistence

SQLite with WAL and single writer queue.

### Analyzer

Baseline, regression, trend, thresholds.

### Report

Markdown, HTML, PDF, CSV.

## Runtime Flow

1.  User starts test.
2.  Django creates TestRun.
3.  Test Runner executes independently.
4.  Adapter controls device.
5.  Collectors sample metrics.
6.  Queue buffers data.
7.  Batch writer stores SQLite.
8.  Analyzer updates runtime state.
9.  Dashboard displays summarized live status.
10. Report generated.

## Communication

Control: REST. Live status: Polling -\> SSE -\> WebSocket (future). Raw
metrics stay inside framework.

## Database

devices test_cases test_runs metric_samples test_events test_results
failure_artifacts

## Demo

Windows Laptop + Samsung + Google TV + scrcpy.

Run test → Phone controlled → Live metrics → Inject failure → Report →
Baseline comparison.

## Roadmap

v0.1 ADB + SQLite v0.2 Dashboard v0.3 Adapter v0.4 Baseline v0.5 Google
TV v0.6 Redis/Celery v0.7 AI Report v1.0 Device Farm

## Philosophy

The Dashboard is not the Framework.

The Framework must continue collecting data even if the UI closes.

Focus on reliable data generation, persistence, and recovery.
