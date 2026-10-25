@echo off
rem ===========================================================================
rem  flow-pipeline - start the dashboard
rem
rem  Everything this project needs lives in one tool folder (D:\Tools by
rem  default, created by setup.bat). This script points Python, ffmpeg, gflow
rem  and every cache at that folder, so it works even if the C: drive is full
rem  and even if another Python is installed on PATH.
rem
rem  Double-click to run. The window stays open if the server exits.
rem ===========================================================================

setlocal EnableExtensions
title flow-pipeline dashboard
chcp 65001 >nul 2>nul

set "TOOLS=D:\Tools"
if not "%FLOWPIPE_TOOLS_DIR%"=="" set "TOOLS=%FLOWPIPE_TOOLS_DIR%"
set "PY=%TOOLS%\Python311\python.exe"

if not exist "%PY%" goto :no_python

rem ---- tools first on PATH, so the project always uses its own copies -------
set "PATH=%TOOLS%\ffmpeg\bin;%TOOLS%\Python311\Scripts;%TOOLS%\Python311;%PATH%"
set "PYTHONUTF8=1"
set "PIP_CACHE_DIR=%TOOLS%\pip-cache"
set "TMP=%TOOLS%\tmp"
set "TEMP=%TOOLS%\tmp"
set "PLAYWRIGHT_BROWSERS_PATH=%TOOLS%\playwright-browsers"
set "GFLOW_CLI_HOME=%TOOLS%\gflow"
set "FLOWPIPE_TOOLS_DIR=%TOOLS%"
if "%FLOWPIPE_DATA_DIR%"=="" set "FLOWPIPE_DATA_DIR=%TOOLS%\data"

rem ---- server settings (change the port here if 8000 is taken) -------------
set "DRIVER_LABEL=%FLOWPIPE_DRIVER%"
if "%DRIVER_LABEL%"=="" set "DRIVER_LABEL=mock - offline, no Google account needed"
if "%FLOWPIPE_HOST%"=="" set "FLOWPIPE_HOST=127.0.0.1"
if "%FLOWPIPE_PORT%"=="" set "FLOWPIPE_PORT=8000"

if not exist "%FLOWPIPE_DATA_DIR%" mkdir "%FLOWPIPE_DATA_DIR%"
if not exist "%TOOLS%\tmp" mkdir "%TOOLS%\tmp"

cd /d "%~dp0"

echo ============================================================
echo  flow-pipeline
echo ============================================================
echo  dashboard : http://localhost:%FLOWPIPE_PORT%
echo  data      : %FLOWPIPE_DATA_DIR%
echo  tools     : %TOOLS%
echo  driver    : %DRIVER_LABEL%
echo.
echo  Local only. Set FLOWPIPE_HOST=0.0.0.0 to reach it from another
echo  machine on your network.
echo  Press Ctrl+C to stop the server.
echo ============================================================
echo.

"%PY%" -m flowpipe.main --host %FLOWPIPE_HOST% --port %FLOWPIPE_PORT% --open
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" goto :crashed
echo Server stopped.
exit /b 0

:crashed
echo [ERROR] The server exited with code %RC%.
echo.
echo Try the self test to see what is wrong:
echo   "%PY%" "%~dp0scripts\selftest.py" --tools "%TOOLS%"
echo.
pause
exit /b %RC%

:no_python
echo ============================================================
echo  Python was not found
echo ============================================================
echo  Expected: %PY%
echo.
echo  Run setup.bat first. It installs Python, ffmpeg and every
echo  package this project needs into %TOOLS%
echo ============================================================
pause
exit /b 1
