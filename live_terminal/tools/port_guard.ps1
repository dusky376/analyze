# Port guard for the one-click launcher.
#
# Why process start-time is NOT used:
#   Measured 2026-11-16 on Windows:
#     - Process A (old code) holds 8765 but Get-Process cannot see it
#       (different session / elevated). netstat still lists its PID.
#     - Process B (new code) starts later and IS visible, so a
#       "start time > source mtime" test calls it current.
#     - Requests are actually answered by A, so the page stays stale.
#   Process bookkeeping is unreliable. The only trustworthy signal is the
#   build id the service reports about itself over HTTP.
#
# Modes:
#   -Probe  (default) print one token, no side effects:
#             current | stale | none | killed | stale-unremovable:IDS
#   -Ensure           do the whole job and return an exit code for start.bat:
#             0 = port already serves CURRENT code -> caller may just open browser
#             1 = port free / was freed            -> caller should start server
#             3 = blocked by an unremovable listener (message already printed)
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\port_guard.ps1 -Probe
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\port_guard.ps1 -Ensure
#
# NOTE: keep this file ASCII-only. PowerShell 5.1 reads BOM-less files as ANSI,
#       and non-ASCII comments break parsing.

param(
    [int]$Port = 8765,
    [switch]$Probe,
    [switch]$Ensure
)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Get-CodeBuild {
    (Get-ChildItem -Path 'app.py', 'web', 'backend' -Recurse -File `
            -Include *.py, *.js, *.css, *.html |
        Measure-Object -Property LastWriteTime -Maximum).Maximum
}

function Get-Listeners {
    $lines = netstat -ano | Select-String ":$Port" | Select-String 'LISTENING'
    @($lines | ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique)
}

function Get-ServedBuild {
    try {
        $v = Invoke-RestMethod "http://127.0.0.1:$Port/api/version" -TimeoutSec 8
        return [double]$v.build
    } catch {
        return $null
    }
}

$mtime = Get-CodeBuild
$want = ([datetimeoffset]$mtime).ToUnixTimeMilliseconds() / 1000

function Get-State {
    $listeners = Get-Listeners
    if (-not $listeners -or $listeners.Count -eq 0) { return 'none' }
    $build = Get-ServedBuild
    if ($null -ne $build -and [math]::Abs($build - $want) -le 5) { return 'current' }
    return 'stale'
}

function Invoke-Kill {
    $listeners = Get-Listeners
    foreach ($p in $listeners) {
        $pr = Get-Process -Id $p -ErrorAction SilentlyContinue
        if ($pr) {
            try { Stop-Process -Id $p -Force -ErrorAction Stop } catch { }
        }
    }
    Start-Sleep -Milliseconds 900
    $left = Get-Listeners
    if (-not $left -or $left.Count -eq 0) { return 'killed' }
    return ('stale-unremovable:' + (($left -join ',')))
}

# ---------------------------------------------------------------- probe mode
if (-not $Ensure) {
    Write-Output (Get-State)
    exit 0
}

# --------------------------------------------------------------- ensure mode
$state = Get-State

if ($state -eq 'current') {
    Write-Host "[INFO] Port $Port is already serving the current build."
    exit 0
}

if ($state -eq 'none') {
    Write-Host "[INFO] Port $Port is free."
    exit 1
}

# state == 'stale'
Write-Host "[INFO] Port $Port is served by an OUTDATED instance. Stopping it..."
$after = Invoke-Kill
Write-Host "[INFO] stop result: $after"

if ($after -eq 'killed') {
    Write-Host "[INFO] Old instance stopped, port is free."
    exit 1
}

# unremovable
$ids = ($after -replace '^stale-unremovable:', '')
Write-Host ""
Write-Host "============================================================" -ForegroundColor Yellow
Write-Host " [WARN] An OLD server still holds port $Port and cannot be" -ForegroundColor Yellow
Write-Host "        stopped from this window (PID $ids)." -ForegroundColor Yellow
Write-Host ""
Write-Host "        The page would keep showing outdated content, so the" -ForegroundColor Yellow
Write-Host "        launcher stops here instead of starting a second" -ForegroundColor Yellow
Write-Host "        server on the same port." -ForegroundColor Yellow
Write-Host ""
Write-Host " Fix, in an ADMIN PowerShell / cmd:" -ForegroundColor Yellow
Write-Host "        taskkill /F /PID $ids" -ForegroundColor White
Write-Host " Then run start.bat again." -ForegroundColor Yellow
Write-Host ""
Write-Host " If that PID is unknown, close the other console window that" -ForegroundColor Yellow
Write-Host " is running 'python app.py' and retry." -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Yellow
Write-Host ""
exit 3
