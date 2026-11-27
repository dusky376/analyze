@echo off
rem ===========================================================================
rem  WUKONG ZHENJING TERMINAL - START  (E5:B1 two-board queue strategy)
rem
rem  Usage:
rem    start.bat               start and open browser
rem    start.bat --no-browser  start without opening browser
rem    start.bat --daemon      hidden daemon: auto-restart on crash
rem    start.bat --check       probe python only
rem    start.bat --status      show service / port state
rem
rem  Stop: stop.bat
rem  ASCII-ONLY ON PURPOSE (cmd.exe parses batch with the GBK code page).
rem ===========================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "WEBPORT=8765"
set "HEALTH=http://127.0.0.1:%WEBPORT%/api/version"

if not exist "live_terminal\app.py" (
  echo [ERROR] live_terminal\app.py not found. Keep this file next to live_terminal\.
  pause
  exit /b 1
)
cd /d "%~dp0live_terminal"

rem ---- python probe -------------------------------------------------------
set "PY="
if defined WUKONG_PYTHON (
  "%WUKONG_PYTHON%" -c "import flask, polars, requests" >nul 2>nul
  if not errorlevel 1 set "PY=%WUKONG_PYTHON%"
)
if not defined PY if exist "C:\ProgramData\anaconda3\python.exe" (
  "C:\ProgramData\anaconda3\python.exe" -c "import flask, polars, requests" >nul 2>nul
  if not errorlevel 1 set "PY=C:\ProgramData\anaconda3\python.exe"
)
if not defined PY (
  python -c "import flask, polars, requests" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [ERROR] no Python found with flask + polars + requests.
  echo         set WUKONG_PYTHON to your interpreter and retry.
  pause
  exit /b 1
)

echo [INFO] python: %PY%
if /i "%~1"=="--check" (
  echo [OK] environment check passed.
  exit /b 0
)
if /i "%~1"=="--status" goto :status

echo ============================================================
echo   WUKONG TERMINAL - start    %DATE% %TIME%
echo ============================================================
echo.

set "DAEMON=0"
set "OPENBROWSER=1"
if /i "%~1"=="--daemon" set "DAEMON=1"
if /i "%~1"=="--no-browser" set "OPENBROWSER=0"

rem port_guard: 0=current 1=free-or-freed 3=unremovable listener
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\port_guard.ps1" -Port %WEBPORT% -Ensure
set "GUARD=%ERRORLEVEL%"
if "%GUARD%"=="0" (
  echo [OK] already serving the current build.
  goto :ready
)
if "%GUARD%"=="3" (
  echo [ERROR] port %WEBPORT% is held by a listener this script cannot kill.
  echo         netstat -ano ^| findstr :%WEBPORT%
  pause
  exit /b 3
)

if "%DAEMON%"=="1" (
  echo [INFO] daemon mode: hidden, auto-restart on crash.
  start "Wukong daemon" /min powershell -NoProfile -ExecutionPolicy Bypass -File "tools\daemon.ps1" -Port %WEBPORT%
  goto :ready
)

echo [INFO] launching server ...
start "Wukong terminal" /min cmd /c ""%PY%" app.py"

echo [INFO] waiting for service ...
set "W=0"
:wait
ping -n 3 127.0.0.1 >nul
set /a W+=2
"%PY%" -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('%HEALTH%',timeout=3).status==200 else 1)" >nul 2>nul
if not errorlevel 1 goto :ready
if !W! lss 120 goto :wait

echo [WARN] no answer after 120s. Running it here so the traceback is visible:
"%PY%" app.py
goto :fail

:ready
echo.
echo ============================================================
echo   READY
echo   board : http://127.0.0.1:%WEBPORT%
echo   stop  : stop.bat
echo ============================================================
echo.
if "%OPENBROWSER%"=="1" start "" "http://127.0.0.1:%WEBPORT%"
pause
endlocal
exit /b 0

:status
echo service:
"%PY%" -c "import urllib.request,json;print('  up  ',json.loads(urllib.request.urlopen('%HEALTH%',timeout=3).read()))" 2>nul || echo   down
echo.
echo listeners on %WEBPORT%:
netstat -ano | findstr :%WEBPORT%
pause
endlocal
exit /b 0

:fail
endlocal
exit /b 1
