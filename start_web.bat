@echo off
REM ===========================================================================
REM  Paper Trader - start the web app on this computer (Windows)
REM
REM  Double-click this file. Then open  http://127.0.0.1:8080  in any browser.
REM  Closing this window stops the server.
REM
REM  Any arguments are passed straight to app_web.py, for example:
REM      start_web.bat --host 0.0.0.0          reachable from other devices
REM      start_web.bat --port 9000             a different port
REM      start_web.bat --reset-password        print a new password and exit
REM ===========================================================================
setlocal
cd /d "%~dp0"

set PYEXE=py
where py >nul 2>nul
if errorlevel 1 set PYEXE=python

%PYEXE% --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python was not found. Install Python 3.10 or newer from python.org
  echo and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

REM The market-data layer needs these two; install them once if missing.
%PYEXE% -c "import pandas, websocket" >nul 2>nul
if errorlevel 1 (
  echo Installing runtime dependencies ^(pandas, websocket-client^)...
  %PYEXE% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Could not install the dependencies. Run this manually to see why:
    echo     %PYEXE% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
)

echo.
echo   Starting the paper trader. The trading engine runs inside this server,
echo   so it keeps scanning whether or not a browser is open.
echo.

%PYEXE% app_web.py %*
echo.
echo   Server stopped.
pause
