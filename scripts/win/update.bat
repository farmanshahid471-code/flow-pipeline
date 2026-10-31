@echo off
rem ===========================================================================
rem  flow-pipeline - fetch the latest build into a fresh folder
rem
rem  One short command, because doing this by hand goes wrong: a long URL is
rem  mangled when pasted into cmd.exe, the zip still has to be extracted, and the
rem  extracted folder carries an awkward archive name.
rem
rem  usage:
rem    scripts\win\update.bat                 -> D:\flowtest
rem    scripts\win\update.bat D:\flowtest2    -> anywhere else
rem
rem  Your existing folder is never touched, and neither is D:\Tools: projects,
rem  settings, the Google sign-in, VoiceStudio and its models all stay where they
rem  are. This only puts a fresh copy of the CODE on disk.
rem ===========================================================================
setlocal EnableExtensions
title flow-pipeline - update
chcp 65001 >nul 2>nul

set "TOOLS=D:\Tools"
if not "%FLOWPIPE_TOOLS_DIR%"=="" set "TOOLS=%FLOWPIPE_TOOLS_DIR%"
set "PY=%TOOLS%\Python311\python.exe"
set "DEST=%~1"
if "%DEST%"=="" set "DEST=D:\flowtest"

echo ============================================================
echo  flow-pipeline - update
echo ============================================================
echo  tools : %TOOLS%
echo  dest  : %DEST%
echo ============================================================
echo.

if not exist "%PY%" goto :no_python

"%PY%" "%~dp0..\fetch_update.py" --dest "%DEST%"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" goto :ok
if "%RC%"=="2" goto :offline
echo [warn] The download worked but the folder is not usable - read the [warn] lines above.
echo        If %DEST% already exists and is not empty, delete it or pick another name:
echo          scripts\win\update.bat D:\flowtest2
goto :end_fail

:ok
echo  Next:
echo    1. close the OLD dashboard window first
echo    2. start the new one:  %DEST%\run.bat
echo    3. in the browser: Accounts -^> Check session
echo    4. Settings -^> Driver: driver = gflow, gflow_home = %TOOLS%\gflow
echo.
echo  Your projects and settings live in %TOOLS%\data and are untouched by this.
goto :end_ok

:offline
echo [ERROR] Could not download the archive - no internet, or GitHub is unreachable.
echo         Nothing was changed. Try again.
goto :end_fail

:no_python
echo [ERROR] No Python at %PY%
echo         Run setup.bat first.
goto :end_fail

:end_ok
echo.
pause
exit /b 0

:end_fail
echo.
pause
exit /b 1
