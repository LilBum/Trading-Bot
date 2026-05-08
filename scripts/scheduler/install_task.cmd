@echo off
REM ========================================================================
REM install_task.cmd  -  Register the trading-bot daily launcher with
REM                       Windows Task Scheduler.
REM
REM Schedules run_live_paper.cmd to fire at 5:00 AM local time (=8 AM ET
REM if the machine is on Pacific) on weekdays. The launcher itself drives
REM the session, so this just needs to wake the process up.
REM
REM Run this once, with administrative privileges so /rl HIGHEST takes.
REM
REM Verify with:    schtasks /query /tn TradingBotLivePaper
REM Remove with:    uninstall_task.cmd
REM ========================================================================

setlocal
set _LAUNCHER=%~dp0run_live_paper.cmd

echo Registering scheduled task "TradingBotLivePaper" ...
echo   Launcher: %_LAUNCHER%
echo   Schedule: weekdays @ 05:00 local time

schtasks /create /tn "TradingBotLivePaper" ^
    /tr "\"%_LAUNCHER%\"" ^
    /sc weekly ^
    /d MON,TUE,WED,THU,FRI ^
    /st 05:00 ^
    /rl HIGHEST ^
    /f

if errorlevel 1 (
    echo.
    echo Task registration FAILED. Common causes:
    echo   - Not running as Administrator (required for /rl HIGHEST)
    echo   - Existing task with the same name in a different state
    echo Re-run from an elevated cmd.exe prompt.
    endlocal & exit /b 1
)

echo.
echo Task registered. View it with:
echo   schtasks /query /tn TradingBotLivePaper /v /fo LIST
endlocal & exit /b 0
