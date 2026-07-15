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
- `cli.py`: headless control surface, also used by the dashboard's process-spawn trigger

Run tests without third-party dependencies: `python -m unittest discover -s tests -v`.

## Dashboard (v0.2)

A Django + Vue dashboard reads the same `autoperf.db` directly (WAL mode allows
concurrent readers alongside the CLI's writer) and can trigger new runs without
blocking on the request -- runs are spawned as independent OS processes via
`autoperf.workers._device_worker`, the same mechanism `run-many` uses, so
`TestRunner`'s SIGINT handling (which requires the main thread of the main
interpreter) works correctly and a run outlives the dev server if it restarts.

```powershell
.\venv\Scripts\python.exe -m pip install -e .[dashboard]
.\venv\Scripts\python.exe webapp\manage.py runserver 8000
```
In a second terminal:
```powershell
cd webapp\frontend
npm install
npm run dev
```
Open `http://localhost:5173` -- refresh devices, pick one, start a run, and
watch status/metrics update via polling. Run the API tests with
`python webapp\manage.py test dashboard`.
