# AutoPerf

AutoPerf v0.1 is an offline-first Android performance test core. It runs independently of a dashboard, samples Android devices through ADB, and persists metrics through a single batch writer into SQLite WAL.

## Quick start (Windows PowerShell)

```powershell
.\venv\Scripts\python.exe -m pip install -e .
adb devices
autoperf devices
autoperf run --serial <DEVICE_SERIAL> --duration 60
autoperf run --serial <DEVICE_SERIAL> --duration 60 --app com.android.settings
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
- `cli.py`: headless control surface (`devices`, `run`, `run-many`, `status`, `baseline set/show`, `compare`)

Run tests without third-party dependencies: `python -m unittest discover -s tests -v`.

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
