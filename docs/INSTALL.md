# Installing AutoPerf (Windows, macOS, Ubuntu/WSL)

AutoPerf has three independent layers you can install separately:

| Layer | What it needs | Optional? |
|---|---|---|
| **Core CLI** (`autoperf ...`) | Python 3.11+, `adb` on PATH | Required |
| **Dashboard** (Django + Vue) | + Node.js 18+, Redis, Celery | Optional |
| **Live screen** (`/screen`) | nothing extra beyond core | Optional |

Everything talks to the phone through `adb`, so `adb` must be reachable
regardless of which layers you install.

## Prerequisites

| Tool | Windows | macOS | Ubuntu / WSL |
|---|---|---|---|
| Python 3.11+ | [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.12` | `brew install python@3.12` | `sudo apt install python3.12 python3.12-venv` |
| Node.js 18+ (dashboard only) | `winget install OpenJS.NodeJS.LTS` | `brew install node` | `sudo apt install nodejs npm` |
| Android platform-tools (`adb`) | `winget install Google.PlatformTools` | `brew install android-platform-tools` | `sudo apt install android-tools-adb` |
| Redis (dashboard only) | Docker Desktop | `brew install redis` or Docker Desktop | `sudo apt install redis-server` |

After installing `adb`, plug in a phone with **USB debugging** enabled
(Settings -> Developer options -> USB debugging) and confirm the RSA
prompt on the device, then check:

```
adb devices
```

The device should show `device` (not `unauthorized` or `offline`).
**On WSL specifically, this usually will NOT work over USB** -- see the
[WSL / Ubuntu](#manual-steps-wsl--ubuntu-bashzsh) section below.

## Automated setup

Three options, pick whichever fits how you're setting the machine up.

**Any OS (Python, detects the platform for you):**
```bash
python3 scripts/setup.py
# or, to also auto-install missing adb/node/redis (winget/Homebrew/apt):
python3 scripts/setup.py --install-deps
```
This is the recommended entry point -- one script for Windows, macOS,
native Linux, and WSL. It detects which platform it's running on
(including WSL specifically, via `WSL_DISTRO_NAME`/`/proc/version`) and
picks the right package manager and commands itself. It only needs
whatever Python you already have to invoke it with; it finds (or tells
you to install) a Python >= 3.11 for the actual venv separately.

**Windows (PowerShell), platform-specific script:**
```powershell
.\scripts\setup.ps1
# or, to also auto-install missing adb/node/redis via winget:
.\scripts\setup.ps1 -InstallDeps
```

**macOS (bash/zsh), platform-specific script:**
```bash
./scripts/setup.sh
# or, to also auto-install missing adb/node/redis via Homebrew:
./scripts/setup.sh --install-deps
```

All three scripts are safe to re-run -- they skip steps that are already
done. By default they only *check* for `adb`/`node`/`redis` and print
install instructions; nothing outside the repo (venv, `node_modules`) is
touched unless you pass `-InstallDeps` / `--install-deps`.

## Manual steps (Windows, PowerShell)

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -e .[dashboard,worker,livescreen,dev]

adb devices
.\venv\Scripts\python.exe -m autoperf devices

# Dashboard only:
docker run -d --name autoperf-redis -p 6379:6379 redis:7-alpine
.\venv\Scripts\python.exe webapp\manage.py runserver 8000
# (new terminal)
.\venv\Scripts\python.exe scripts\start-worker.py
# (new terminal)
cd webapp\frontend
npm install
npm run dev
```

## Manual steps (macOS, bash/zsh)

```bash
python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -e .[dashboard,worker,livescreen,dev]

adb devices
./venv/bin/autoperf devices

# Dashboard only (pick one Redis option):
brew services start redis
# -- or --
docker run -d --name autoperf-redis -p 6379:6379 redis:7-alpine

./venv/bin/python webapp/manage.py runserver 8000
# (new terminal)
./venv/bin/python scripts/start-worker.py
# (new terminal)
cd webapp/frontend
npm install
npm run dev
```

Note `scripts/start-worker.py` runs a different Celery command here than
on Windows: `--pool=solo` is only *required* on Windows, which has no
`fork()` and therefore no working `prefork` pool. On macOS the default
`prefork` pool works fine and is faster under load (see "Sizing worker
parallelism" below for how the two differ); `--pool=solo` also still works
if you prefer it directly (see `webapp/dashboard/tasks.py`'s docstring for
why `--pool=solo` exists at all -- it's about `TestRunner.run()`'s SIGINT
handling needing the worker's main thread, not about Windows specifically).

## Manual steps (WSL / Ubuntu, bash/zsh)

Same as macOS, but via `apt` instead of `brew`, and one extra wrinkle:
**WSL2 has no native USB passthrough.** A phone plugged into the Windows
host's USB port will not appear in `adb devices` run *inside* WSL, even
though the same phone works fine with `adb` running natively on Windows.

Pick one of these instead of fighting USB passthrough:

- **ADB over WiFi (recommended)** -- no USB involved at all. On the phone,
  enable Settings -> Developer options -> Wireless debugging, then:
  ```bash
  adb pair <phone-ip>:<pairing-port> <6-digit-code>   # one-time
  adb connect <phone-ip>:<port>                       # each session
  ```
  AutoPerf's `adb.py` has `pair()`/`connect()` helpers for exactly this
  (also exposed by the CLI/dashboard).
- **usbipd-win** -- install [usbipd-win](https://github.com/dorssel/usbipd-win)
  on the *Windows* host, then `usbipd attach --wsl --busid <id>` each time
  you plug the phone in, so WSL's `adb` sees it as a native USB device.
- **Keep `adb` on Windows, point WSL at it** -- run `adb.exe` on the
  Windows host (it sees the USB device natively) and have WSL's `adb`
  client talk to it over TCP: `adb -H <windows-host-ip> -P 5037 devices`.

Once a device shows up in `adb devices`, the rest is identical to macOS:

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv android-tools-adb

python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -e .[dashboard,worker,livescreen,dev]

adb devices
./venv/bin/autoperf devices

# Dashboard only:
sudo apt install -y redis-server nodejs npm
sudo service redis-server start   # WSL usually has no systemd; `service` works either way

./venv/bin/python webapp/manage.py runserver 8000
# (new terminal)
./venv/bin/python scripts/start-worker.py
# (new terminal)
cd webapp/frontend
npm install
npm run dev
```

Like macOS, `--pool=solo` is not required here -- the default `prefork`
pool works since WSL is a real Linux kernel underneath.

## Sizing worker parallelism

`scripts/start-worker.py` (run with the venv's own Python, same one you
installed AutoPerf into) decides how many tasks can run at once from
`min(cpu_count, connected_adb_device_count)` -- no point starting 8 workers
on an 8-core laptop with only 2 phones plugged in. If zero devices are
connected when it starts, it defaults to 1 and tells you to either plug a
device in and re-run, or pass `--concurrency N` to force a number.

- **macOS/Linux/WSL**: one `celery -A config worker --concurrency=N -l info`
  process -- `prefork`'s own concurrency already parallelizes across N
  forked children.
- **Windows**: `--pool=solo` can't take an internal concurrency value, so
  N>1 launches N separate `--pool=solo -n workerK@%h` processes instead,
  supervised by the script (Ctrl+C stops all of them).

Either way, two runs can never race against the *same* device even with
N>1: `TestRunner.run()` atomically claims its device before starting
(`Storage.try_start_run()`), and a run queued for a device that's already
busy just retries every couple of seconds until it's free, the same way it
always implicitly waited behind AutoPerf's old single-worker queue --
`scripts/start-worker.py` only changes how many *different* devices can be
tested at once, not the one-run-per-device-at-a-time guarantee.

Override the auto-detected number any time with:
```
python scripts/start-worker.py --concurrency 4
```

## Verifying the install

```
adb devices                                          # phone shows up as "device"
autoperf devices                                     # same list, via AutoPerf
autoperf run --serial <SERIAL> --duration 10          # short smoke test
```

Open `http://localhost:5173` for the dashboard (requires the Django
server, Celery worker, and Redis all running per above).
