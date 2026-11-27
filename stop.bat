@echo off
rem ===========================================================================
rem  WUKONG ZHENJING TERMINAL - STOP
rem
rem  Stops:
rem    - the hidden daemon (first, or it restarts the server)
rem    - the web server listening on 8765
rem
rem  USAGE
rem    stop.bat            stop and pause
rem    stop.bat --quiet    no pause
rem
rem  ASCII-ONLY ON PURPOSE (cmd.exe parses batch with the GBK code page).
rem ===========================================================================
setlocal
cd /d "%~dp0"
if not exist "live_terminal\app.py" (
  echo [ERROR] live_terminal\app.py not found.
  if /i not "%~1"=="--quiet" pause
  exit /b 1
)
cd /d "%~dp0live_terminal"

echo ============================================================
echo   WUKONG TERMINAL - stop         %DATE% %TIME%
echo ============================================================
echo.

echo [1/2] stop daemon ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -match 'daemon.ps1' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [2/2] free port 8765 ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%P >nul 2>nul
)

echo.
echo [OK] stopped.
if /i "%~1"=="--quiet" (
  endlocal
  exit /b 0
)
echo.
pause
endlocal
exit /b 0
