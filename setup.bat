@echo off
rem ===========================================================================
rem  flow-pipeline setup for Windows
rem
rem  Installs EVERYTHING into a single folder on the D: drive (D:\Tools by
rem  default) so the C: drive is left alone:
rem
rem    %TOOLS%\Python311           portable Python 3.11 with pip
rem    %TOOLS%\ffmpeg              ffmpeg + ffprobe
rem    %TOOLS%\pip-cache, tmp      caches, so pip and ffmpeg work off D:
rem    %TOOLS%\gflow               gflow profiles / browser sessions
rem    %TOOLS%\playwright-browsers Playwright browsers, if gflow is installed
rem    %TOOLS%\data                pipeline data: projects, database, settings
rem
rem  The app finds these automatically (see flowpipe/toolchain.py), so future
rem  updates keep using the same install. Safe to run again at any time: every
rem  step is skipped if it is already done.
rem
rem  Usage:  double-click setup.bat
rem          or:  setup.bat            from a console
rem          or:  set FLOWPIPE_TOOLS_DIR=E:\Tools  and run it, to use another drive
rem ===========================================================================

setlocal EnableExtensions
title flow-pipeline setup
color 0B
chcp 65001 >nul 2>nul

set "TOOLS=D:\Tools"
if not "%FLOWPIPE_TOOLS_DIR%"=="" set "TOOLS=%FLOWPIPE_TOOLS_DIR%"

set "PYDIR=%TOOLS%\Python311"
set "PY=%PYDIR%\python.exe"
set "DL=%TOOLS%\downloads"
set "PYZIP=%DL%\python-3.11.9-embed-amd64.zip"
set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"
set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
set "PIP_URL=https://bootstrap.pypa.io/get-pip.py"

echo ============================================================
echo  flow-pipeline setup
echo ============================================================
echo  Install location : %TOOLS%
echo  Project folder   : %REPO%
echo.
echo  Nothing is installed on the C: drive.
echo  This window stays open when the setup finishes.
echo ============================================================
echo.

if not exist "%REPO%\requirements.txt" goto :no_repo
if exist "D:\." goto :have_d
echo [ERROR] Drive D: was not found on this machine.
echo.
echo         Open setup.bat in Notepad and change the line
echo             set "TOOLS=D:\Tools"
echo         to a folder on a drive you do have, then run it again.
goto :fail
:have_d

if not exist "%TOOLS%" mkdir "%TOOLS%"
if not exist "%TOOLS%" goto :cant_mkdir
call :make_dirs

rem ---- keep every temporary file and cache on the tools drive ---------------
set "PYTHONUTF8=1"
set "PIP_CACHE_DIR=%TOOLS%\pip-cache"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PIP_NO_INPUT=1"
set "TMP=%TOOLS%\tmp"
set "TEMP=%TOOLS%\tmp"
set "PLAYWRIGHT_BROWSERS_PATH=%TOOLS%\playwright-browsers"
set "GFLOW_CLI_HOME=%TOOLS%\gflow"
set "FLOWPIPE_TOOLS_DIR=%TOOLS%"
set "FLOWPIPE_DATA_DIR=%TOOLS%\data"

rem ===========================================================================
echo [1/6] Python 3.11  (portable, no installer, no registry, no admin)
echo.
if exist "%PY%" goto :python_ok
call :download "%PY_URL%" "%PYZIP%"
if errorlevel 1 goto :fail
if not exist "%PYDIR%" mkdir "%PYDIR%"
call :unzip "%PYZIP%" "%PYDIR%"
if errorlevel 1 goto :fail
if not exist "%PYDIR%\Lib\site-packages" mkdir "%PYDIR%\Lib\site-packages"
if not exist "%PYDIR%\Scripts" mkdir "%PYDIR%\Scripts"
rem the embeddable distribution needs site-packages switched on, otherwise
rem pip installs into a folder Python does not look at
> "%PYDIR%\python311._pth" echo python311.zip
>>"%PYDIR%\python311._pth" echo .
>>"%PYDIR%\python311._pth" echo Lib\site-packages
>>"%PYDIR%\python311._pth" echo import site
if not exist "%PY%" goto :fail
:python_ok
"%PY%" -c "import sys; print('      python', sys.version.split()[0], 'ready at', sys.executable)"
if errorlevel 1 goto :fail

rem ===========================================================================
echo.
echo [2/6] pip
echo.
"%PY%" -c "import pip" >nul 2>nul
if not errorlevel 1 goto :pip_ok
call :download "%PIP_URL%" "%DL%\get-pip.py"
if errorlevel 1 goto :fail
"%PY%" "%DL%\get-pip.py" --no-warn-script-location
if errorlevel 1 goto :fail
:pip_ok
"%PY%" -m pip install --upgrade --no-warn-script-location pip setuptools wheel
if errorlevel 1 goto :fail

rem ===========================================================================
echo.
echo [3/6] Python packages
echo.
"%PY%" -m pip install --no-warn-script-location --prefer-binary -r "%REPO%\requirements.txt"
if errorlevel 1 goto :fail

rem ===========================================================================
echo.
echo [4/6] ffmpeg
echo.
if exist "%TOOLS%\ffmpeg\bin\ffmpeg.exe" goto :ffmpeg_ok
"%PY%" "%REPO%\scripts\win\install_ffmpeg.py" --tools "%TOOLS%"
if errorlevel 1 goto :fail
:ffmpeg_ok
"%TOOLS%\ffmpeg\bin\ffmpeg.exe" -version >nul 2>nul
if errorlevel 1 goto :ffmpeg_broken
echo       ffmpeg ready: %TOOLS%\ffmpeg\bin\ffmpeg.exe
goto :ffmpeg_done
:ffmpeg_broken
echo [ERROR] ffmpeg is in place but does not run.
echo         Delete %TOOLS%\ffmpeg and run setup.bat again.
goto :fail
:ffmpeg_done

rem ===========================================================================
echo.
echo [5/6] Google Flow driver - optional
echo.
echo       gflow-cli is what talks to Google Flow in a real browser.
echo       You only need it if you want to generate with your own account.
echo       It is a large download and needs a Google sign-in afterwards.
echo.
set "ANS="
set /p "ANS=      Install gflow-cli now? [y/N]: "
if /i not "%ANS%"=="y" goto :gflow_skip
echo.
echo       installing gflow-cli, this can take a few minutes...
"%PY%" -m pip install --no-warn-script-location --prefer-binary gflow-cli
if errorlevel 1 goto :gflow_failed
if not exist "%PYDIR%\Scripts\gflow.exe" goto :gflow_noexe
echo.
echo       gflow-cli installed. Sign in once per Google account:
echo         "%PYDIR%\Scripts\gflow.exe" auth login --profile studio-a
echo       then add that profile name on the Accounts page of the dashboard.
echo.
echo       If gflow asks for a browser, install one on the D: drive with:
echo         "%PY%" -m playwright install chromium
goto :gflow_done
:gflow_noexe
echo       [warn] gflow-cli installed but gflow.exe was not created.
echo       Run it as: "%PY%" -m gflow --help
goto :gflow_done
:gflow_failed
echo.
echo       [warn] gflow-cli did not install. The pipeline still works offline
echo       with the mock driver. Retry later with:
echo         "%PY%" -m pip install gflow-cli
goto :gflow_done
:gflow_skip
echo       skipped - the dashboard starts with the offline mock driver.
echo       install it later with: "%PY%" -m pip install gflow-cli
:gflow_done

rem ===========================================================================
echo.
echo [6/6] Verifying the install
echo.
echo       Running the whole pipeline offline with placeholder media.
echo       No Google account and no credits are used.
echo.
"%PY%" "%REPO%\scripts\selftest.py" --tools "%TOOLS%"
if errorlevel 1 goto :selftest_failed

echo.
echo ============================================================
echo  Setup complete
echo ============================================================
echo  Start the dashboard : double-click run.bat
echo  Then open           : http://localhost:8000
echo.
echo  Tools  : %TOOLS%
echo  Data   : %TOOLS%\data
echo  Python : %PY%
echo.
echo  Next: add ANTHROPIC_API_KEY to your environment for headless
echo  manifest generation, or leave it and use the manual Claude
echo  Desktop path from the dashboard.
echo ============================================================
pause
endlocal
exit /b 0

rem ===========================================================================
rem  error paths - every one of them pauses, so the window never just closes
rem ===========================================================================
:no_repo
echo [ERROR] requirements.txt was not found next to setup.bat.
echo         Put setup.bat in the flow-pipeline folder and run it again.
goto :fail

:cant_mkdir
echo [ERROR] Could not create %TOOLS%
echo         Check that the drive exists, is writable and has free space.
goto :fail

:selftest_failed
echo.
echo ============================================================
echo  Setup finished, but the self test reported failures.
echo ============================================================
echo  Look for the lines marked [FAIL] above. The tool paths and
echo  project folder are the usual suspects.
echo.
echo  You can re-run just the test with:
echo    "%PY%" "%REPO%\scripts\selftest.py" --tools "%TOOLS%"
echo ============================================================
pause
endlocal
exit /b 1

:fail
echo.
echo ============================================================
echo  Setup did not finish
echo ============================================================
echo  Everything downloaded so far is kept, so you can simply run
echo  setup.bat again to retry.
echo ============================================================
pause
endlocal
exit /b 1

rem ===========================================================================
rem  subroutines
rem ===========================================================================

:make_dirs
if not exist "%DL%" mkdir "%DL%"
if not exist "%TOOLS%\tmp" mkdir "%TOOLS%\tmp"
if not exist "%TOOLS%\pip-cache" mkdir "%TOOLS%\pip-cache"
if not exist "%TOOLS%\gflow" mkdir "%TOOLS%\gflow"
if not exist "%TOOLS%\playwright-browsers" mkdir "%TOOLS%\playwright-browsers"
if not exist "%TOOLS%\data" mkdir "%TOOLS%\data"
exit /b 0

:download
rem %1 = url, %2 = destination file
where /q curl
if not errorlevel 1 goto :download_curl
goto :download_ps
:download_curl
echo       downloading %~nx2
curl -L --fail --retry 3 --connect-timeout 20 --progress-bar -o "%~2" "%~1"
if errorlevel 1 echo       [warn] curl failed for %~1
exit /b %errorlevel%
:download_ps
echo       downloading %~nx2  (powershell)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri '%~1' -OutFile '%~2'"
if errorlevel 1 echo       [warn] powershell download failed for %~1
exit /b %errorlevel%

:unzip
rem %1 = zip file, %2 = destination folder
where /q tar
if not errorlevel 1 goto :unzip_tar
goto :unzip_ps
:unzip_tar
tar -xf "%~1" -C "%~2"
exit /b %errorlevel%
:unzip_ps
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Force -LiteralPath '%~1' -DestinationPath '%~2'"
exit /b %errorlevel%
