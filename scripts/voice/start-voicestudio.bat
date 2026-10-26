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

if not exist "%VS%\backend\main.py" goto :not_installed

rem ---- already running? ----------------------------------------------------
if not exist "%PY%" goto :probe_with_curl
"%PY%" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:%VOICE_PORT%/health', timeout=2)" >nul 2>nul
if errorlevel 1 goto :start_it
echo  VoiceStudio is already running on port %VOICE_PORT%.
echo  Point the dashboard at it: Settings - tts_provider = voicestudio
echo ============================================================
pause
exit /b 0

:probe_with_curl
curl -s -m 2 "http://127.0.0.1:%VOICE_PORT%/health" >nul 2>nul
if not errorlevel 1 goto :already_running_curl

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
echo  Run setup.bat (step 7 installs VoiceStudio), or install uv:
echo    "%PY%" -m pip install uv
echo ============================================================
pause
exit /b 1

:already_running_curl
echo  VoiceStudio is already running on port %VOICE_PORT%.
echo ============================================================
pause
exit /b 0

:not_installed
echo  VoiceStudio is not installed yet.
echo.
echo  Run setup.bat and let step 7 finish, or install it directly:
echo    "%PY%" "%TOOLS%\..\flow-pipeline\scripts\win\install_voice.py" --tools "%TOOLS%"
echo.
echo  The pipeline still works without it - narration just stays off.
echo ============================================================
pause
exit /b 1
