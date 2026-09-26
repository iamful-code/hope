@echo off
rem ---------------------------------------------------------------------------
rem hope: start everything - the monitor in a separate minimized window (opens
rem the browser) and the live paper engine in this window.
rem Usage:  windows\start.cmd [hope run options]
rem   no options          strategy from STRATEGY_CONFIG in .env
rem   -c path\config.yaml another strategy config
rem   --duration 3600     stop after N seconds
rem Stop: Ctrl+C in this window, then close the "hope monitor" window.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title hope engine
set "RC=0"
call "%~dp0_env.cmd" || goto :end
if not exist "%HOPE_EXE%" goto :not_installed

start "hope monitor" /min cmd /c call "%~dp0monitor.cmd"

echo [hope] monitor: http://127.0.0.1:%MONITOR_PORT%/  - window "hope monitor"
echo [hope] engine: default strategy config %STRATEGY_CONFIG%, results in data\hope.db
echo [hope] stop the engine with Ctrl+C
echo.
"%HOPE_EXE%" run --restart %*
set "RC=%ERRORLEVEL%"
echo.
echo [hope] engine stopped with code %RC%. The monitor keeps running - close its window when done.
goto :end

:not_installed
echo [hope] hope is not installed yet - run windows\install.cmd first.
set "RC=1"
:end
if not defined HOPE_NO_PAUSE if not defined CI pause
endlocal & exit /b %RC%
