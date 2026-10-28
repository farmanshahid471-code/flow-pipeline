@echo off
rem ===========================================================================
rem  flow-pipeline - install and sign in to gflow-cli (Google Flow)
rem
rem  Everything the "mock -> real" switch needs, in one re-runnable place:
rem    1. gflow-cli installed
rem    2. the Chromium it drives, inside D:\Tools (never C:)
rem    3. a signed-in Google account profile  ->  gflow auth login
rem    4. a credit reading, so you know the sign-in worked
rem
rem  usage:  scripts\win\flow_login.bat [profile-name]
rem          double-click it, or run it from cmd; the window never closes
rem          without telling you what to do next.
rem
rem  Run it again whenever a session expires (the dashboard marks the account
rem  needs_auth) or when you want to add another Google account.
rem ===========================================================================

setlocal EnableExtensions
title flow-pipeline - Google Flow sign-in
chcp 65001 >nul 2>nul

set "TOOLS=D:\Tools"
if not "%FLOWPIPE_TOOLS_DIR%"=="" set "TOOLS=%FLOWPIPE_TOOLS_DIR%"
if "%DOWNLOADS%"=="" set "DOWNLOADS=D:\downloads"
set "PYDIR=%TOOLS%\Python311"
set "PY=%PYDIR%\python.exe"
set "GFLOW=%PYDIR%\Scripts\gflow.exe"

rem ---- the same paths run.bat uses, so profiles are found later -------------
set "PLAYWRIGHT_BROWSERS_PATH=%TOOLS%\playwright-browsers"
set "GFLOW_CLI_HOME=%TOOLS%\gflow"
set "GFLOW_CLI_OUTPUT_DIR=%DOWNLOADS%\flow"
set "TMP=%TOOLS%\tmp"
set "TEMP=%TOOLS%\tmp"
set "PIP_CACHE_DIR=%TOOLS%\pip-cache"
set "PYTHONUTF8=1"

if not exist "%TOOLS%" mkdir "%TOOLS%"
if not exist "%GFLOW_CLI_HOME%" mkdir "%GFLOW_CLI_HOME%"
if not exist "%GFLOW_CLI_OUTPUT_DIR%" mkdir "%GFLOW_CLI_OUTPUT_DIR%"

echo ============================================================
echo  flow-pipeline - Google Flow sign-in
echo ============================================================
echo  tools      : %TOOLS%
echo  profiles   : %GFLOW_CLI_HOME%
echo  browser    : %PLAYWRIGHT_BROWSERS_PATH%
echo  downloads  : %GFLOW_CLI_OUTPUT_DIR%
echo ============================================================
echo.

if not exist "%PY%" goto :no_python

rem ---- 1. gflow-cli -------------------------------------------------------
"%GFLOW%" --version >nul 2>nul
if not errorlevel 1 goto :have_gflow
echo [1/4] Installing gflow-cli...
"%PY%" -m pip install --no-warn-script-location --prefer-binary gflow-cli
if errorlevel 1 goto :install_failed
if not exist "%GFLOW%" goto :no_exe
goto :gflow_ready

:install_failed
echo.
echo [ERROR] pip could not install gflow-cli.
echo         Check the internet connection, then run this file again.
goto :end_fail

:no_exe
echo.
echo [warn] gflow-cli installed, but %GFLOW% was not created.
echo        Run it as:  "%PY%" -m gflow --help
echo        and put that in Settings - gflow_bin if the dashboard cannot find it.
goto :end_fail

:have_gflow
echo [1/4] gflow-cli already installed.
:gflow_ready
"%GFLOW%" --version

rem ---- 2. the browser it drives -------------------------------------------
echo.
echo [2/4] Checking the browser...
if exist "%PLAYWRIGHT_BROWSERS_PATH%\chromium*" goto :have_browser
dir /b /ad "%PLAYWRIGHT_BROWSERS_PATH%\chromium*" >nul 2>nul
if not errorlevel 1 goto :have_browser
echo       Downloading Chromium into %PLAYWRIGHT_BROWSERS_PATH%
echo       (about 150 MB, a few minutes on a slow line)...
"%PY%" -m playwright install chromium
if not errorlevel 1 goto :browser_ok
echo.
echo [warn] playwright could not install Chromium. gflow may use
echo        patchright instead - trying that as a fallback...
"%PY%" -m patchright install chromium
if not errorlevel 1 goto :browser_ok
echo.
echo [warn] No browser could be installed automatically.
echo        If gflow later fails with "browser not found", install one by hand:
echo          "%PY%" -m playwright install chromium
echo        and check that PLAYWRIGHT_BROWSERS_PATH points at %PLAYWRIGHT_BROWSERS_PATH%
goto :signin
:browser_ok
echo       Browser ready.
goto :signin
:have_browser
echo       Browser already installed.

rem ---- 3. sign in ----------------------------------------------------------
:signin
echo.
echo [3/4] Google sign-in
echo.
echo       A browser window opens. Sign in to the Google account you want to
echo       use for Flow - by hand, including any 2FA or verification page.
echo       Nothing here types a password or solves a challenge for you.
echo.
set "PROFILE=%~1"
if not "%PROFILE%"=="" goto :have_profile
set /p "PROFILE=      Profile name [studio-a]: "
if "%PROFILE%"=="" set "PROFILE=studio-a"
:have_profile
echo.
echo       Profile: %PROFILE%
echo       (use exactly this name on the Accounts page of the dashboard)
echo.
"%GFLOW%" auth login --profile %PROFILE%
if errorlevel 1 goto :login_failed

rem ---- 4. prove the sign-in worked ----------------------------------------
echo.
echo [4/4] Reading the credit balance for %PROFILE% ...
"%GFLOW%" credits user --profile %PROFILE% --json
if errorlevel 1 goto :credits_failed
echo.
echo ============================================================
echo  Signed in as profile "%PROFILE%".
echo.
echo  Next, in the dashboard (run.bat):
echo    1. Accounts  -^>  Add account
echo         label   : anything, e.g. Studio A
echo         profile : %PROFILE%
echo    2. press "Refresh credits" - the row must show a number
echo    3. Settings -^> Driver
echo         driver           = gflow
echo         gflow_home       = %GFLOW_CLI_HOME%
echo         credits_per_clip = 20   (Veo Fast; Lite 10, Quality 100)
echo         image_model      = leave empty
echo         video_model_preference = leave empty
echo    4. the header pill must turn green: driver: gflow
echo.
echo  Verify the command surface before spending a credit:
echo    "%PY%" scripts\check_gflow.py --profile %PROFILE%
echo.
echo  Add another Google account: run this file again with a new name
echo    "%GFLOW%" auth login --profile studio-b
echo ============================================================
goto :end_ok

:login_failed
echo.
echo [ERROR] The sign-in did not finish.
echo.
echo  - close the browser window and run this file again
echo  - if it says the profile is locked, close any other gflow or Chrome
echo    window using that profile, then retry
echo  - if the window never opened, the browser install failed - scroll up
echo  The pipeline is untouched by this; it still runs on the mock driver.
goto :end_fail

:credits_failed
echo.
echo [warn] Signed in, but the credit check failed.
echo        That usually means the session did not persist - run this file
echo        again, and make sure GFLOW_CLI_HOME is %GFLOW_CLI_HOME% in the
echo        shell that starts the dashboard too (run.bat sets it for you).
goto :end_fail

:no_python
echo [ERROR] No Python at %PY%
echo.
echo         Run setup.bat first - it installs Python and everything else on
echo         the D: drive, and this file needs it.
goto :end_fail

:end_ok
echo.
pause
exit /b 0

:end_fail
echo.
echo ============================================================
echo  Nothing was changed in the dashboard. The mock driver still works.
echo ============================================================
pause
exit /b 1
