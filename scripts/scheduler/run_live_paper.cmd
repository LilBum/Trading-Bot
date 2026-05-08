@echo off
REM ========================================================================
REM run_live_paper.cmd  -  Daily launcher for the trading bot live runner.
REM
REM Invoked by Windows Task Scheduler at 5:00 AM Pacific (= 8 AM ET) Mon-Fri.
REM Activates the project root, runs the live CLI, redirects stdout/stderr
REM to a timestamped log under logs\live\.
REM
REM This script does NOT exit on its own - the Python CLI runs until the
REM session ends at 16:00 ET (default) and then disconnects from IBKR.
REM ========================================================================

setlocal

REM cd to project root (this script lives in scripts\scheduler\, so go up two)
cd /d "%~dp0..\..\"

REM Ensure log directory exists
if not exist "logs\live" mkdir "logs\live"

REM Build a date-stamped log filename: YYYYMMDD-HHMMSS
for /f "tokens=2 delims==" %%I in ('"wmic os get localdatetime /value"') do set _DT=%%I
set _STAMP=%_DT:~0,8%-%_DT:~8,6%
set _LOG=logs\live\live_%_STAMP%.log

echo [%date% %time%] Launching live CLI ^> %_LOG%

REM Run the CLI with NQ signal -> MNQ execution defaults.
REM Override flags here for a different routing.
py -3.14 -m src.futures_execution.live_cli ^
    --signal-symbol NQ ^
    --execution-symbol MNQ ^
    --tick-seconds 30 ^
    --journal-path "logs\live\journal_%_STAMP%.jsonl" ^
    > "%_LOG%" 2>&1

set _EXIT=%errorlevel%
echo [%date% %time%] CLI exited with code %_EXIT% >> "%_LOG%"
endlocal & exit /b %_EXIT%
