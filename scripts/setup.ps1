#Requires -Version 5.1
<# Windows entry point for the canonical cross-platform installer. #>
param(
    [switch]$InstallDeps,
    [switch]$SkipVerify,
    [ValidateSet("auto", "wsl", "docker")]
    [string]$RedisBackend = "auto",
    [string]$WslDistro
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = $null
$pythonPrefix = @()
foreach ($candidate in @(
    @{ Command = "py"; Prefix = @("-3") },
    @{ Command = "python"; Prefix = @() }
)) {
    if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
    $versionText = & $candidate.Command @($candidate.Prefix) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $versionText -and [version]$versionText -ge [version]"3.11") {
        $python = $candidate.Command
        $pythonPrefix = $candidate.Prefix
        break
    }
}
if (-not $python) {
    Write-Error "Python 3.11 or newer is required. Install it with: winget install Python.Python.3.12"
}
$arguments = @("scripts/setup.py")
if ($InstallDeps) { $arguments += "--install-deps" }
if ($SkipVerify) { $arguments += "--skip-verify" }
$arguments += @("--redis-backend", $RedisBackend)
if ($WslDistro) { $arguments += @("--wsl-distro", $WslDistro) }
& $python @pythonPrefix @arguments
exit $LASTEXITCODE
