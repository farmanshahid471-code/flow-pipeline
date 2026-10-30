@echo off
rem ===========================================================================
rem  flow-pipeline - start the local VoiceStudio server (text-to-speech)
rem
rem  VoiceStudio lives in D:\Tools\voicestudio and is reached only over
rem  loopback (127.0.0.1:3900). Everything it writes - its database, engines and
rem  the multi-gigabyte model cache - stays on the D: drive.
rem
rem  Leave this window open while you generate narration, then press Ctrl+C.
rem  The pipeline works without it: narration is simply off, and word timings
rem  fall back to proportional spacing.
rem ===========================================================================

setlocal EnableExtensions
title flow-pipeline voice server
chcp 65001 >nul 2>nul

set "TOOLS=D:\Tools"
if not "%FLOWPIPE_TOOLS_DIR%"=="" set "TOOLS=%FLOWPIPE_TOOLS_DIR%"
set "VS=%TOOLS%\voicestudio"
set "PY=%TOOLS%\Python311\python.exe"
if not "%VOICE_PORT%"=="" goto :port_set
set "VOICE_PORT=3900"
:port_set

rem ---- everything VoiceStudio writes goes to D: ----------------------------
set "HF_HOME=%TOOLS%\hf-cache"
set "HF_HUB_CACHE=%TOOLS%\hf-cache"
set "TORCH_HOME=%TOOLS%\torch-cache"
set "UV_CACHE_DIR=%TOOLS%\uv-cache"
set "OMNIVOICE_DATA_DIR=%TOOLS%\voicestudio-data"
set "OMNIVOICE_CACHE_DIR=%TOOLS%\hf-cache"
set "PIP_CACHE_DIR=%TOOLS%\pip-cache"
set "TMP=%TOOLS%\tmp"
set "TEMP=%TOOLS%\tmp"
set "PYTHONUTF8=1"
set "PATH=%TOOLS%\uv;%TOOLS%\Python311;%TOOLS%\Python311\Scripts;%PATH%"

echo ============================================================
echo  flow-pipeline voice server
echo ============================================================
echo  VoiceStudio : %VS%
echo  address     : http://127.0.0.1:%VOICE_PORT%
echo  model cache : %HF_HOME%
echo  data        : %OMNIVOICE_DATA_DIR%
echo ============================================================
echo.

rem ---- is one already running? ---------------------------------------------
rem  Checked before the local copy: the VoiceStudio desktop app (or a server you
rem  started earlier) answers on the same port and needs no copy under %TOOLS%.
if not exist "%PY%" goto :probe_with_curl
"%PY%" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:%VOICE_PORT%/health', timeout=2)" >nul 2>nul
if errorlevel 1 goto :probe_with_curl
goto :already_running

:probe_with_curl
curl -s -m 2 "http://127.0.0.1:%VOICE_PORT%/health" >nul 2>nul
if not errorlevel 1 goto :already_running_curl

rem ---- nothing on the port: we need a local copy to start ----------------
if not exist "%VS%\backend\main.py" goto :not_installed
goto :start_it

:start_it
echo  Starting the backend. The first synthesis downloads the voice model
echo  (a few GB into %HF_HOME%) and takes a couple of minutes.
echo  Press Ctrl+C here to stop it.
echo.
pushd "%VS%"
if exist "%VS%\.venv\Scripts\python.exe" goto :use_venv
where uv >nul 2>nul
if errorlevel 1 goto :no_uv
uv run uvicorn main:app --app-dir backend --host 127.0.0.1 --port %VOICE_PORT%
set "RC=%ERRORLEVEL%"
popd
goto :ended

:use_venv
"%VS%\.venv\Scripts\python.exe" -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port %VOICE_PORT%
set "RC=%ERRORLEVEL%"
popd
goto :ended

:ended
echo.
if not "%RC%"=="0" goto :failed
echo  Voice server stopped.
pause
exit /b 0

:failed
echo [ERROR] The voice server exited with code %RC%.
echo.
echo  Read the lines above - usually a missing model or a busy port.
echo  Try another port:   set VOICE_PORT=3901
echo  Re-run setup.bat to repair the install.
echo ============================================================
pause
exit /b %RC%

:no_uv
popd
echo [ERROR] Neither %VS%\.venv nor uv was found.
echo.
echo  Run setup.bat (step 6 installs VoiceStudio), or install uv:
echo    "%PY%" -m pip install uv
echo ============================================================
pause
exit /b 1

:already_running_curl
:already_running
echo  VoiceStudio is already running on port %VOICE_PORT%.
echo  That can be the VoiceStudio desktop app or a server started earlier -
echo  either way there is nothing to start here.
echo.
echo  Point the dashboard at it: Settings - tts_provider = voicestudio
echo  (url http://127.0.0.1:%VOICE_PORT%, set automatically)
echo ============================================================
pause
exit /b 0

:not_installed
echo  Nothing is answering on port %VOICE_PORT% and there is no local copy at
echo  %VS%
echo.
echo  If you already use the VoiceStudio desktop app, just open it - this
echo  project only talks to it over http://127.0.0.1:%VOICE_PORT% and does not
echo  need its own copy. Then set tts_provider = voicestudio in Settings.
echo.
echo  Otherwise let setup.bat step 6 install it, or run:
echo    "%PY%" "%~dp0..\win\install_voice.py" --tools "%TOOLS%"
echo  To reuse an install kept elsewhere:
echo    "%PY%" "%~dp0..\win\install_voice.py" --tools "%TOOLS%" --voicestudio-dir "D:\VoiceStudioApp"
echo.
echo  The pipeline still works without it - narration just stays off.
echo ============================================================
pause
exit /b 1
