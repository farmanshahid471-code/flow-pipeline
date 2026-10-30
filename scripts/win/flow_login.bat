@echo off
rem ===========================================================================
rem  flow-pipeline - install and sign in to gflow-cli (Google Flow)
rem
rem  Everything the "mock -> real" switch needs, in one re-runnable place:
rem    1. gflow-cli installed
rem    2. the Chrome it drives, inside D:\Tools (never C:)
rem    3. a signed-in Google profile -> gflow auth login --browser chrome
rem    4. the session PROVED with `gflow auth status`
rem    5. a credit reading, so you know Flow can bill
rem
rem  usage:  scripts\win\flow_login.bat [profile-name]
rem          double-click it, or run it from cmd; the window never closes
rem          without telling you what to do next.
rem
rem  Run it again whenever a session expires (the dashboard marks the account
rem  needs_auth or needs_human) or when you want to add another Google account.
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
rem the same data dir run.bat uses, so we can default to the profile the
rem dashboard actually has an account for
if "%FLOWPIPE_DATA_DIR%"=="" set "FLOWPIPE_DATA_DIR=%TOOLS%\data"

if not exist "%TOOLS%" mkdir "%TOOLS%"
if not exist "%GFLOW_CLI_HOME%" mkdir "%GFLOW_CLI_HOME%"
if not exist "%TMP%" mkdir "%TMP%"
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
echo [1/5] Installing gflow-cli...
"%PY%" -m pip install --no-warn-script-location --prefer-binary gflow-cli
if errorlevel 1 goto :install_failed
if not exist "%GFLOW%" goto :no_exe
goto :gflow_ready

:have_gflow
echo [1/5] gflow-cli already installed.
:gflow_ready
"%GFLOW%" --version

rem ---- 2. the browser it drives -------------------------------------------
echo.
echo [2/5] Checking the browser...
dir /b /ad "%PLAYWRIGHT_BROWSERS_PATH%\chromium*" >nul 2>nul
if not errorlevel 1 goto :have_browser
echo       Downloading Chromium into %PLAYWRIGHT_BROWSERS_PATH%
echo       (about 150 MB, a few minutes on a slow line)...
"%PY%" -m playwright install chromium
if not errorlevel 1 goto :have_browser
echo.
echo [warn] playwright could not install Chromium.
echo        If gflow later fails with "browser not found", install one by hand:
echo          "%PY%" -m playwright install chromium

:have_browser
echo       Browser ready.

rem ---- 3. which profile ---------------------------------------------------
echo.
echo [3/5] Which profile?
echo.
set "PROFILE=%~1"
if not "%PROFILE%"=="" goto :have_profile

rem Signing in under a name the dashboard does not use is the classic trap: the
rem browser shows the account you expect (Chrome shares its cookies), the login
rem reports success, and the run keeps using the old, expired session.
"%PY%" "%~dp0..\gflow_profiles.py" > "%TMP%\flow_profiles.txt" 2>nul
if not exist "%TMP%\flow_profiles.txt" goto :ask_profile
set "PROFILE="
for /f "usebackq delims=" %%p in ("%TMP%\flow_profiles.txt") do @if not defined PROFILE set "PROFILE=%%p"
if not defined PROFILE goto :ask_profile
echo       Profiles in the dashboard:
type "%TMP%\flow_profiles.txt"
del "%TMP%\flow_profiles.txt" >nul 2>nul
echo.
set /p "PROFILE_IN=      Profile name [%PROFILE%]: "
if not "%PROFILE_IN%"=="" set "PROFILE=%PROFILE_IN%"
goto :have_profile

:ask_profile
del "%TMP%\flow_profiles.txt" >nul 2>nul
set /p "PROFILE=      Profile name [farman]: "
if "%PROFILE%"=="" set "PROFILE=farman"

:have_profile
echo.
echo       Profile: %PROFILE%
echo       (use exactly this name on the Accounts page of the dashboard)

rem ---- 4. sign in ---------------------------------------------------------
echo.
echo [4/5] Google sign-in
echo.
echo       A browser window opens. Sign in to the Google account you want to
echo       use for Flow - by hand, including any 2FA or verification page.
echo       Nothing here types a password or solves a challenge for you.
echo.
rem --browser chrome is not cosmetic: a profile created by bundled Chromium
rem carries no chrome strategy marker, and generation later opens it with
rem bundled Chromium, which Google rejects (401 on create) even though the
rem session still reads fine for credits.
"%GFLOW%" auth login --browser chrome --profile %PROFILE%
if errorlevel 1 goto :login_failed

rem ---- 5. prove the session, then read the balance ------------------------
echo.
echo [5/5] Proving the session with "gflow auth status" (spends nothing, ~45s)...
"%GFLOW%" auth status --profile %PROFILE%
if errorlevel 1 goto :status_failed
echo.
echo       Session verified.

echo.
echo       Reading the credit balance for %PROFILE% ...
"%GFLOW%" credits user --profile %PROFILE% --json
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :credits_ok
if "%RC%"=="3" goto :credits_session
if "%RC%"=="8" goto :credits_session
if "%RC%"=="12" goto :credits_session
if "%RC%"=="31" goto :credits_flowapp
if "%RC%"=="28" goto :credits_flowapp
goto :credits_other

rem ---- outcomes -----------------------------------------------------------
:credits_ok
echo.
echo ============================================================
echo  Signed in as profile "%PROFILE%".
echo.
echo  Next, in the dashboard (run.bat):
echo    1. Accounts -^> Add account
echo         label   : anything, e.g. Studio A
echo         profile : %PROFILE%      (exactly this)
echo    2. press "Check session" - it must say verified
echo    3. press "Refresh credits" - the row must show a number
echo    4. Settings -^> Driver
echo         driver           = gflow
echo         gflow_home       = %GFLOW_CLI_HOME%
echo         credits_per_clip = 20   (Veo Fast; Lite 10, Quality 100)
echo         image_model      = leave empty
echo         video_model_preference = leave empty
echo    5. the header pill must turn green: driver: gflow
echo.
echo  Verify the command surface before spending a credit:
echo    "%PY%" scripts\check_gflow.py --profile %PROFILE%
echo.
echo  Add another Google account: run this file again with a new name
echo    "%GFLOW%" auth login --browser chrome --profile studio-b
echo.
echo  Profiles that hold a session right now:
"%GFLOW%" auth list
echo ============================================================
goto :end_ok

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

:status_failed
echo.
echo [warn] The sign-in finished, but the session does not verify.
echo.
echo  gflow explained the reason above. The usual two:
echo    - browser strategy marker missing -> the login must use real Chrome;
echo      this file already passes --browser chrome, so run it again
echo    - cookie store unreadable          -> close every Chrome window, retry
echo.
echo  Which profiles hold a session:
"%GFLOW%" auth list
echo.
echo  The run uses the profile named on the Accounts page: they must match.
goto :end_fail

:credits_session
echo.
echo [warn] The session did not persist - the CLI asked for a sign-in again.
echo.
echo  Run this file once more. If it keeps happening, check that
echo  GFLOW_CLI_HOME is %GFLOW_CLI_HOME% in the shell that starts the
echo  dashboard too (run.bat sets it for you).
goto :end_fail

:credits_flowapp
echo.
echo [warn] Sign-in is FINE - Flow's own balance reply could not be read.
echo.
echo        Exit code %RC% means the page loaded but the balance did not come
echo        back in time ("no readable nzlxg reply"). That is a Flow-side
echo        change, not your account: the CLI marks it retryable.
echo.
echo  What to do:
echo    1. retry once:   "%GFLOW%" credits user --profile %PROFILE% --json
echo    2. still failing? open flow.google.com in a browser and read the
echo       balance from the account menu yourself.
echo    3. try a newer CLI:   "%PY%" -m pip install -U gflow-cli
echo.
echo  You can carry on without the number: the dashboard shows the balance
echo  as unknown, and an unknown balance cannot be checked before a clip.
goto :credits_ok

:credits_other
echo.
echo [warn] Signed in, but the credit check failed with exit code %RC%.
echo        Run the command again to see the full error:
echo          "%GFLOW%" credits user --profile %PROFILE% --json
goto :credits_ok

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
