# Signal Desk service daemon (Bug-30, 2026-11-17).
#
# Symptom that motivated this script:
#   On 2026-11-17 18:00 the 8765 process was found DEAD (page unreachable,
#   no crash trace in logs). Nothing restarted it; the user discovered the
#   outage by opening the page. A closing-hours service must not depend on
#   a console window staying alive.
#
# What this daemon does (single loop, one job):
#   every CHECK_INTERVAL seconds:
#     1. probe http://127.0.0.1:8765/api/version
#          - HTTP OK  -> if served build is stale vs disk -> restart (code update)
#                       else -> healthy, keep waiting
#          - dead     -> start the server (with backoff on repeated failures)
#     2. append one line to tools\daemon.log on every state change
#
# Reuses tools\port_guard.ps1 for probe/kill logic (-Probe mode) so there is
# exactly ONE implementation of "is the running server current".
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\daemon.ps1           # foreground
#   powershell -NoProfile -WindowStyle Hidden -File tools\daemon.ps1              # hidden
#   start.bat --daemon                                                            # recommended
#
# Stop: close the window, or `Get-Process powershell | where CommandLine -match daemon` kill.
# NOTE: keep this file ASCII-only (GBK console codepage breaks non-ASCII comments).

param(
    [int]$Port = 8765,
    [int]$CheckIntervalSec = 30,
    [int]$MaxBackoffSec = 300
)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$logfile = Join-Path $root "tools\daemon.log"

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    $line | Out-File -FilePath $logfile -Append -Encoding utf8
    Write-Host $line
}

# ---- interpreter pick (same priority as start.bat, ASCII-only) ----
$py = $null
if ($env:LIVE_TERMINAL_PYTHON -and `
    (& $env:LIVE_TERMINAL_PYTHON -c "import flask, polars, pandas, requests" 2>$null) -eq $null) {
    $py = $env:LIVE_TERMINAL_PYTHON
}
if (-not $py) {
    foreach ($c in @("C:\ProgramData\anaconda3\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
        if (Test-Path $c) {
            & $c -c "import flask, polars, pandas, requests" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $py = $c; break }
        }
    }
}
if (-not $py) {
    python -c "import flask, polars, pandas, requests" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $py = "python" }
}
if (-not $py) {
    Write-Log "FATAL: no python with flask/polars/pandas/requests found. Exiting."
    exit 1
}
Write-Log "daemon started. port=$Port interval=${CheckIntervalSec}s python=$py pid=$PID"

$backoff = 0
$lastState = ""

function Get-ServedBuild {
    try {
        $v = Invoke-RestMethod "http://127.0.0.1:$Port/api/version" -TimeoutSec 8
        return @{ ok = $true; build = [double]$v.build; stale = [bool]$v.stale; pid = $v.pid }
    } catch {
        return @{ ok = $false }
    }
}

while ($true) {
    $v = Get-ServedBuild

    if ($v.ok -and -not $v.stale) {
        # healthy + current
        if ($lastState -ne "healthy") {
            Write-Log "healthy: pid=$($v.pid) build=$($v.build)"
            $lastState = "healthy"
        }
        $backoff = 0
        Start-Sleep -Seconds $CheckIntervalSec
        continue
    }

    if ($v.ok -and $v.stale) {
        # serving OLD code -> restart to pick up updates (same guarantee as
        # start.bat's port_guard, but automatic instead of on next double-click)
        Write-Log "stale build detected (served=$($v.build)) -> restarting server"
        & powershell -NoProfile -ExecutionPolicy Bypass -File "tools\port_guard.ps1" -Ensure -Port $Port | Out-Null
        Start-Sleep -Seconds 2
    }

    # dead (or just killed) -> (re)start with backoff
    $sleepSec = $CheckIntervalSec + $backoff
    Write-Log "server down -> starting (next check in ${sleepSec}s, backoff=${backoff}s)"
    $p = Start-Process -FilePath $py -ArgumentList "app.py" `
         -WorkingDirectory $root -WindowStyle Hidden -PassThru `
         -RedirectStandardOutput (Join-Path $root "data\daemon_server.out.log") `
         -RedirectStandardError  (Join-Path $root "data\daemon_server.err.log")
    Write-Log "started server pid=$($p.Id)"
    $lastState = "restarting"

    Start-Sleep -Seconds 10
    $v2 = Get-ServedBuild
    if ($v2.ok) {
        Write-Log "restart OK: pid=$($v2.pid)"
        $backoff = 0
    } else {
        $backoff = [math]::Min($backoff + 30, $MaxBackoffSec)
        Write-Log "restart NOT answering yet; backoff -> ${backoff}s"
        # kill the half-started process so next loop starts clean
        & powershell -NoProfile -ExecutionPolicy Bypass -File "tools\port_guard.ps1" -Ensure -Port $Port | Out-Null
    }
    Start-Sleep -Seconds $sleepSec
}
