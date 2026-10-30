@echo off
rem ===========================================================================
rem  flow-pipeline - start again from a clean slate
rem
rem  usage:
rem    scripts\win\reset.bat          wipe pipeline DATA only (projects,
rem                                   database, settings.json). Keeps D:\Tools:
rem                                   Python, ffmpeg, the signed-in gflow
rem                                   profile, VoiceStudio + models + the
rem                                   accepted model licence.
rem
rem    scripts\win\reset.bat all      wipe D:\Tools as well - a true first run.
rem                                   Costs: ~150 MB Chromium, ~230 Python
rem                                   packages, and the VoiceStudio models
rem                                   (several GB) get downloaded again, and
rem                                   the model licence has to be accepted
rem                                   again. The Google sign-in too.
rem
rem  Nothing here touches the CODE folder: delete or rename that folder
rem  yourself after closing the dashboard, then extract a fresh download.
rem ===========================================================================
setlocal EnableExtensions
title flow-pipeline - reset
chcp 65001 >nul 2>nul

set "TOOLS=D:\Tools"
if not "%FLOWPIPE_TOOLS_DIR%"=="" set "TOOLS=%FLOWPIPE_TOOLS_DIR%"
if "%FLOWPIPE_DATA_DIR%"=="" set "FLOWPIPE_DATA_DIR=%TOOLS%\data"
set "DOWNLOADS=D:\downloads"
if not exist "D:\." set "DOWNLOADS=%TOOLS%\downloads"
if not "%FLOWPIPE_DOWNLOADS_DIR%"=="" set "DOWNLOADS=%FLOWPIPE_DOWNLOADS_DIR%"

echo ============================================================
echo  flow-pipeline - reset
rem ---- safety: this script deletes folders, so refuse to run on anything
rem      that does not look like the install it was written for --------------
if "%TOOLS%"=="" goto :bad_target
if "%FLOWPIPE_DATA_DIR%"=="" goto :bad_target
if "%TOOLS:~1,2%"==":" if "%TOOLS:~2,1%"=="" goto :bad_target
if "%FLOWPIPE_DATA_DIR:~1,2%"==":" if "%FLOWPIPE_DATA_DIR:~2,1%"=="" goto :bad_target
if not exist "%TOOLS%\Python311\python.exe" goto :bad_tools

echo ============================================================
echo  code       : %~dp0..\..
echo  data       : %FLOWPIPE_DATA_DIR%
echo  downloads  : %DOWNLOADS%
echo  tools      : %TOOLS%
echo ============================================================
echo.
echo  Step 1. Close the dashboard window (run.bat) and the voice server
echo          (start-voicestudio.bat). Files in use cannot be deleted.
echo.
pause

if /i "%~1"=="all" goto :everything
if not "%~1"=="" echo [warn] ignoring unknown argument "%~1" - wiping data only.

echo.
echo  This deletes: projects, generated media, the database and settings.json.
echo  It KEEPS: Python, ffmpeg, the gflow profiles (Google sign-in), VoiceStudio,
echo            its models and the accepted model licence.
echo.
set "CONFIRM="
set /p "CONFIRM=  Type reset to continue: "
if /i not "%CONFIRM%"=="reset" goto :cancelled

call :clean_data
if errorlevel 1 goto :end_fail
echo.
echo [ok] Data cleared. The next run.bat starts with no projects.
echo      Settings are back to defaults, so set driver and narration again.
goto :end_ok

:everything
echo.
echo  *** THIS ALSO DELETES D:\Tools ***
echo.
echo  Gone: Python, ffmpeg, Chromium, gflow profiles (the Google sign-in),
echo        VoiceStudio, every downloaded model, the accepted licence.
echo  Kept: your code folder and your downloads folder.
echo.
echo  Expect a full setup again: setup.bat, flow_login.bat, the VoiceStudio
echo  install and several GB of model downloads.
echo.
set "CONFIRM="
set /p "CONFIRM=  Type DELETE EVERYTHING to continue: "
if not "%CONFIRM%"=="DELETE EVERYTHING" goto :cancelled

call :clean_data
if errorlevel 1 goto :end_fail
echo.
echo  Removing %TOOLS% ...
if exist "%TOOLS%" rmdir /s /q "%TOOLS%"
if exist "%TOOLS%" goto :tools_locked
echo [ok] Tools removed.
echo.
echo  Next: extract a fresh download to D:\ and run setup.bat.
goto :end_ok

:tools_locked
echo.
echo [ERROR] %TOOLS% could not be deleted completely.
echo         A process still has files open: close Chrome, any gflow command,
echo         the VoiceStudio window and the dashboard, then run this again.
goto :end_fail

rem ---- helpers -------------------------------------------------------------
:clean_data
echo.
if exist "%FLOWPIPE_DATA_DIR%" rmdir /s /q "%FLOWPIPE_DATA_DIR%"
if exist "%FLOWPIPE_DATA_DIR%" goto :data_locked
echo [ok] Removed %FLOWPIPE_DATA_DIR%
echo.
echo  Leaving %DOWNLOADS% alone - finished videos and Flow downloads live there.
exit /b 0

:data_locked
echo.
echo [ERROR] %FLOWPIPE_DATA_DIR% could not be deleted - something is using it.
echo         Close the dashboard and the voice server, then try again.
exit /b 1

:cancelled
echo.
echo Cancelled. Nothing was deleted.
goto :end_ok

:end_ok
echo.
pause
exit /b 0

:bad_target
echo.
echo [ERROR] The paths for this install look wrong, so NOTHING was deleted:
echo           tools : "%TOOLS%"
echo           data  : "%FLOWPIPE_DATA_DIR%"
echo         Set FLOWPIPE_TOOLS_DIR to the folder setup.bat created (D:\Tools)
echo         and run this again.
goto :end_fail

:bad_tools
echo.
echo [ERROR] %TOOLS% does not contain Python311\python.exe.
echo         That is not the tools folder setup.bat creates, so it will not be
echo         wiped. Point FLOWPIPE_TOOLS_DIR at D:\Tools and try again.
goto :end_fail

:end_fail
echo.
pause
exit /b 1
