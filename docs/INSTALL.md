# Installing AutoPerf (Windows & macOS)

AutoPerf has three independent layers you can install separately:

| Layer | What it needs | Optional? |
|---|---|---|
| **Core CLI** (`autoperf ...`) | Python 3.11+, `adb` on PATH | Required |
| **Dashboard** (Django + Vue) | + Node.js 18+, Redis, Celery | Optional |
| **Live screen** (`/screen`) | nothing extra beyond core | Optional |

Everything talks to the phone through `adb`, so `adb` must be reachable
regardless of which layers you install.

## Prerequisites

| Tool | Windows | macOS |
|---|---|---|
| Python 3.11+ | [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.12` | `brew install python@3.12` |
| Node.js 18+ (dashboard only) | `winget install OpenJS.NodeJS.LTS` | `brew install node` |
| Android platform-tools (`adb`) | `winget install Google.PlatformTools` | `brew install android-platform-tools` |
| Redis (dashboard only) | Docker Desktop, or `redis-server` via WSL | `brew install redis` or Docker Desktop |

After installing `adb`, plug in a phone with **USB debugging** enabled
(Settings -> Developer options -> USB debugging) and confirm the RSA
prompt on the device, then check:

```
adb devices
```

The device should show `device` (not `unauthorized` or `offline`).

## Automated setup

An automation script handles the Python venv, editable install, and
frontend `npm install`, and reports which system tools (`adb`, `node`,
`redis`) are still missing.

**Windows (PowerShell):**
```powershell
.\scripts\setup.ps1
# or, to also auto-install missing adb/node/redis via winget:
.\scripts\setup.ps1 -InstallDeps
```

**macOS (bash/zsh):**
```bash
./scripts/setup.sh
# or, to also auto-install missing adb/node/redis via Homebrew:
./scripts/setup.sh --install-deps
```

Both scripts are safe to re-run -- they skip steps that are already done.
By default they only *check* for `adb`/`node`/`redis` and print install
instructions; nothing outside the repo (venv, `node_modules`) is touched
unless you pass `-InstallDeps` / `--install-deps`.

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
cd webapp
..\venv\Scripts\celery.exe -A config worker --pool=solo -l info
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
cd webapp
../venv/bin/celery -A config worker -l info
# (new terminal)
cd webapp/frontend
npm install
npm run dev
```

Note the Celery command differs from Windows: `--pool=solo` is only
*required* on Windows, which has no `fork()` and therefore no working
`prefork` pool. On macOS the default `prefork` pool works fine and is
faster under load; `--pool=solo` also still works if you prefer it (see
`webapp/dashboard/tasks.py`'s docstring for why `--pool=solo` exists at
all -- it's about `TestRunner.run()`'s SIGINT handling needing the
worker's main thread, not about Windows specifically).

## Verifying the install

```
adb devices                                          # phone shows up as "device"
autoperf devices                                     # same list, via AutoPerf
autoperf run --serial <SERIAL> --duration 10          # short smoke test
```

Open `http://localhost:5173` for the dashboard (requires the Django
server, Celery worker, and Redis all running per above).
