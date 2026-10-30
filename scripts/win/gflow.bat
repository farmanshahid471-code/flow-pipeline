@echo off
rem ===========================================================================
rem  flow-pipeline - run gflow with this project's environment
rem
rem  gflow stores sessions in GFLOW_CLI_HOME. A plain cmd window does not have
rem  that variable, so `gflow auth list` there looks in %LOCALAPPDATA% and finds
rem  nothing - the sign-in done by flow_login.bat (D:\Tools\gflow) looks lost, and
rem  the identical Google account shows up in the browser either way.
rem
rem  Use this wrapper for every hand-typed gflow command:
rem
rem    scripts\win\gflow.bat auth list
rem    scripts\win\gflow.bat auth status --profile farman
rem    scripts\win\gflow.bat auth login --browser chrome --profile farman
rem    scripts\win\gflow.bat credits user --profile farman --json
rem
rem  It sets exactly what run.bat and flow_login.bat set, so the dashboard, the
rem  login script and your own commands all talk about the same profile store.
rem ===========================================================================
setlocal EnableExtensions

set "TOOLS=D:\Tools"
if not "%FLOWPIPE_TOOLS_DIR%"=="" set "TOOLS=%FLOWPIPE_TOOLS_DIR%"
set "PYDIR=%TOOLS%\Python311"
set "GFLOW=%PYDIR%\Scripts\gflow.exe"

set "PLAYWRIGHT_BROWSERS_PATH=%TOOLS%\playwright-browsers"
if "%GFLOW_CLI_HOME%"=="" set "GFLOW_CLI_HOME=%TOOLS%\gflow"
if "%GFLOW_CLI_OUTPUT_DIR%"=="" set "GFLOW_CLI_OUTPUT_DIR=%TOOLS%\downloads\flow"
set "TMP=%TOOLS%\tmp"
set "TEMP=%TOOLS%\tmp"
set "PYTHONUTF8=1"
if not exist "%GFLOW_CLI_HOME%" mkdir "%GFLOW_CLI_HOME%"

if exist "%GFLOW%" goto :run
echo [ERROR] gflow-cli is not installed at "%GFLOW%".
echo         Run setup.bat, or scripts\win\flow_login.bat to install and sign in.
rem pause only when double-clicked; keep the exit code for scripts
if not "%1"=="" exit /b 1
pause
exit /b 1

:run

rem Banner so there is never any doubt which profile store is in use.
echo (profiles: %GFLOW_CLI_HOME%)
"%GFLOW%" %*
exit /b %ERRORLEVEL%
