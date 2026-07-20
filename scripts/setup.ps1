#Requires -Version 5.1
<#
Sets up a local AutoPerf dev environment on Windows: creates ./venv, does an
editable install of all optional extras, and installs the frontend's npm
deps. See docs/INSTALL.md for the full manual walkthrough this mirrors.

By default this only *checks* for adb/Node.js/Redis/ffmpeg (ffmpeg is
optional -- only needed for run screen replay) and prints install
instructions -- it does not touch anything outside the repo. Pass
-InstallDeps to also install these via winget.
#>
param(
    [switch]$InstallDeps
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# --- Python venv -----------------------------------------------------------
Write-Step "Python virtual environment (./venv)"
if (-not (Test-Path "$repoRoot\venv\Scripts\python.exe")) {
    $pythonExe = if (Test-Command "py") { "py" } else { "python" }
    $version = & $pythonExe -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    if (-not $version -or [version]$version -lt [version]"3.11") {
        Write-Warn "Python 3.11+ not found on PATH. Install it first:"
        Write-Warn "  winget install Python.Python.3.12"
        exit 1
    }
    & $pythonExe -m venv "$repoRoot\venv"
    Write-Ok "Created venv (Python $version)"
} else {
    Write-Ok "venv already exists"
}
$venvPython = "$repoRoot\venv\Scripts\python.exe"

# --- Editable install --------------------------------------------------
Write-Step "Installing AutoPerf (editable, all extras)"
& $venvPython -m pip install --upgrade pip | Out-Null
& $venvPython -m pip install -e ".[dashboard,worker,livescreen,dev]"
Write-Ok "Python package installed"

# --- adb ---------------------------------------------------------------
Write-Step "Android platform-tools (adb)"
if (Test-Command "adb") {
    Write-Ok "adb found: $((Get-Command adb).Source)"
} elseif ($InstallDeps) {
    winget install --id Google.PlatformTools -e
} else {
    Write-Warn "adb not found on PATH. Install it with:"
    Write-Warn "  winget install --id Google.PlatformTools"
    Write-Warn "(or re-run this script with -InstallDeps)"
}

# --- Node.js (dashboard only) -------------------------------------------
Write-Step "Node.js (dashboard frontend)"
if (Test-Command "node") {
    Write-Ok "node found: $(node --version)"
    Write-Step "Installing frontend npm dependencies"
    Push-Location "$repoRoot\webapp\frontend"
    try { npm install } finally { Pop-Location }
} elseif ($InstallDeps) {
    winget install --id OpenJS.NodeJS.LTS -e
    Write-Warn "Re-run this script once Node.js is on PATH to install npm deps."
} else {
    Write-Warn "node not found on PATH -- skipping frontend npm install."
    Write-Warn "  winget install --id OpenJS.NodeJS.LTS"
    Write-Warn "(or re-run this script with -InstallDeps, then re-run to pick up npm install)"
}

# --- Redis (dashboard only) ----------------------------------------------
Write-Step "Redis (Celery broker, dashboard only)"
$redisUp = $false
try {
    $probe = Test-NetConnection -ComputerName "localhost" -Port 6379 -WarningAction SilentlyContinue
    $redisUp = $probe.TcpTestSucceeded
} catch {}
if ($redisUp) {
    Write-Ok "Something is already listening on localhost:6379"
} elseif ($InstallDeps -and (Test-Command "docker")) {
    docker run -d --name autoperf-redis -p 6379:6379 redis:7-alpine
    Write-Ok "Started Redis in Docker (container: autoperf-redis)"
} else {
    Write-Warn "No Redis detected on localhost:6379. Start one with:"
    Write-Warn "  docker run -d --name autoperf-redis -p 6379:6379 redis:7-alpine"
}

# --- ffmpeg (optional, run screen replay) -------------------------------
Write-Step "ffmpeg (optional -- run screen replay)"
if (Test-Command "ffmpeg") {
    Write-Ok "ffmpeg found: $((Get-Command ffmpeg).Source)"
} elseif ($InstallDeps) {
    winget install --id Gyan.FFmpeg -e
} else {
    Write-Warn "ffmpeg not found on PATH -- run screen replay will be skipped."
    Write-Warn "  winget install --id Gyan.FFmpeg"
    Write-Warn "(or re-run this script with -InstallDeps)"
}

Write-Step "Done"
Write-Host "Next steps:"
Write-Host "  adb devices"
Write-Host "  .\venv\Scripts\python.exe -m autoperf devices"
Write-Host "See docs/INSTALL.md for how to start the dashboard, Celery worker, and frontend."
