# AutoPerf

AutoPerf v0.1 is an offline-first Android performance test core. It runs independently of a dashboard, samples Android devices through ADB, and persists metrics through a single batch writer into SQLite WAL.

## Quick start (Windows PowerShell)

```powershell
.\venv\Scripts\python.exe -m pip install -e .
adb devices
autoperf devices
autoperf run --serial <DEVICE_SERIAL> --duration 60
autoperf run-many --serial <DEVICE_A> --serial <DEVICE_B> --duration 60
autoperf status <RUN_ID>
```

The initial collectors are total CPU, used memory, battery level, and battery temperature. Collector failures are recorded as events and do not stop the run. Pressing Ctrl+C records an interrupted checkpoint; resume with `autoperf run --serial <DEVICE_SERIAL> --duration 60 --resume <RUN_ID>`.

Collectors execute concurrently in a bounded thread pool. A stuck collector is reported as a timeout and cannot block other sampling schedules. Multi-device runs use one Windows `spawn`-compatible worker process per device; the supervisor records worker crashes and heartbeat timeouts.

## Architecture boundaries

- `adb.py`: safe subprocess boundary and device discovery
- `collectors.py`: plug-in sampling interface
- `runner.py`: lifecycle, scheduling, fault isolation, checkpoints
- `storage.py`: WAL schema and single writer queue
- `cli.py`: headless control surface suitable for later Django integration

Run tests without third-party dependencies: `python -m unittest discover -s tests -v`.
