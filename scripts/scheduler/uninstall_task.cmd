@echo off
REM ========================================================================
REM uninstall_task.cmd  -  Remove the scheduled trading-bot launcher.
REM ========================================================================

setlocal

echo Removing scheduled task "TradingBotLivePaper" ...
schtasks /delete /tn "TradingBotLivePaper" /f

if errorlevel 1 (
    echo Task not found or could not be removed.
    endlocal & exit /b 1
)

echo Task removed.
endlocal & exit /b 0
